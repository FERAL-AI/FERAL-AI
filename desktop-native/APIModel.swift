import SwiftUI
import AppKit

@MainActor final class NativeModel: ObservableObject {
    @Published var ready = false
    @Published private(set) var serviceReachable = false
    @Published var busy = false
    @Published var error: String?
    @Published var chatError: String?
    @Published private(set) var runtimeHealthWarning: String?
    @Published private(set) var recoveryStatus = "Shared conversation recovery has not been verified."
    @Published var startupStatus = "Starting your local agent…"
    @Published var isSending = false
    @Published var codingBusy = false
    @Published var switchingConversation = false
    @Published var uploadingAttachments = false
    @Published var pendingAttachments: [NativeAttachmentRef] = []
    @Published var attachmentError: String?
    @Published var messages: [NativeMessage] = []
    @Published var observedTodos: [[String: Any]]?
    let richChat = NativeRichChatModel()
    @Published var coding = NativeCodingState()
    @Published var memories: [NativeMemory] = []
    @Published var devices: [NativeDevice] = []
    @Published var endpoint = "http://127.0.0.1:11434"
    @Published var modelName = ""
    @Published var displayName = ""
    @Published var avatarChoice = "photo"
    @Published var importedAvatarPath: String?
    @Published var onboarded = false
    // This is presentation state, separate from the backend's setup receipt.
    @Published var showProviderSetup = false
    private let runtime = BrainRuntime()
    private var socket: URLSessionWebSocketTask?
    private var receiveTask: Task<Void, Never>?
    private var codingPoll: Task<Void, Never>?
    private var responseDeadline: Task<Void, Never>?
    private var conversationID = "" {
        didSet { if oldValue != conversationID { appSessionScope = UUID() } }
    }
    private var appSessionScope = UUID()
    private var conversationRevision = UUID()
    private var applyingSnapshotHistory = false
    private var deletedConversationIDs = Set<String>()
    private var codingHandle = ""
    private var streamMessageID: String?
    private var pendingUserMessageID: String?
    private var socketGeneration = UUID()
    private var shuttingDown = false
    private var connecting = false
    private var runtimeRevision = UUID()
    private var conversationSaveTask: Task<Void, Never>?
    private var workspaceCenter: NotificationCenter?
    private var workspaceObservers: [NSObjectProtocol] = []
    private let injectedRuntimeOwner: (() -> NativeRuntimeOwnership?)?

    private lazy var recovery = NativeSessionRecoveryModel(preferences: prefs, transport: { [session] request in
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw NativeFailure("Local session recovery was unavailable.") }
        return (data, http)
    })
    private let prefs: UserDefaults
    private let session: URLSession
    private let transportDelegate = NativeLocalSessionDelegate()
    lazy var voice = NativeVoiceEngine(sendFrame: { [weak self] frame in
        guard let self, self.ready, !self.switchingConversation, let socket = self.socket, socket.state == .running,
              frame["session_id"] as? String == self.conversationID else { throw NativeFailure("Voice is disconnected or the conversation changed.") }
        let data = try JSONSerialization.data(withJSONObject: frame)
        try await socket.send(.string(String(decoding: data, as: UTF8.self)))
    })

    var featureBaseURL: URL? { ready ? runtime.baseURL : nil }
    var securityBaseURL: URL? { serviceReachable || ready ? runtime.baseURL : nil }
    var activeConversationID: String { conversationID }
    // App REST dispatch can run agent turns without Chat turn IDs. Give it
    // a distinct scope so late app replies cannot complete an active Chat turn.
    var appSurfaceSessionID: String? { conversationID.isEmpty ? nil : "native-apps-" + appSessionScope.uuidString }
    var chatMutationBusy: Bool { shuttingDown || isSending || switchingConversation || uploadingAttachments || !["off", "ended"].contains(voice.state) }
    // Applying the reviewed saved point owns its own operation lock. Its
    // thread transition must not invalidate that same review mid-callback.
    var chatToolsHostBusy: Bool { isSending || (switchingConversation && !applyingSnapshotHistory) || uploadingAttachments || !["off", "ended"].contains(voice.state) }

    func restoreThread(_ thread: [String: Any]) throws {
        guard let id = thread["id"] as? String, !id.isEmpty,
              !deletedConversationIDs.contains(id),
              let rows = thread["messages"] as? [[String: Any]] else {
            throw NativeFailure("The agent returned an unreadable conversation.")
        }
        conversationID = id; messages = rows.map(NativeMessage.restored); observedTodos = nil
        richChat.configure(sessionID: id, connectionID: socketGeneration)
    }

    init(session injectedSession: URLSession? = nil, preferences: UserDefaults? = nil, runtimeOwner: (() -> NativeRuntimeOwnership?)? = nil) {
        injectedRuntimeOwner = runtimeOwner
        prefs = preferences ?? UserDefaults(suiteName: ProcessInfo.processInfo.environment["FERAL_NATIVE_PREFS_SUITE"] ?? "ai.feral.native.preview")!
        displayName = prefs.string(forKey: "displayName") ?? ""
        avatarChoice = prefs.string(forKey: "avatarChoice") ?? "photo"
        importedAvatarPath = prefs.string(forKey: "importedAvatarPath")
        onboarded = prefs.bool(forKey: "onboarded")
        showProviderSetup = !prefs.bool(forKey: "onboarded")
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 60
        configuration.timeoutIntervalForResource = 300
        session = injectedSession ?? URLSession(configuration: configuration, delegate: transportDelegate, delegateQueue: nil)
        runtime.onHealthEvent = { [weak self] event in self?.observeRuntimeHealth(event) }
        if injectedSession == nil {
            let center = NSWorkspace.shared.notificationCenter
            workspaceCenter = center
            workspaceObservers.append(center.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in guard let self, !self.shuttingDown else { return }; self.runtime.sleep() }
            })
            workspaceObservers.append(center.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor in guard let self, !self.shuttingDown else { return }; await self.runtime.wake() }
            })
        }
        transportDelegate.onSocketOpen = { [weak self] socket in
            Task { @MainActor in
                guard let self, self.socket === socket else { return }
                self.voice.configureConnection(sessionID: self.conversationID, connected: true)
            }
        }
        transportDelegate.onSocketClose = { [weak self] socket in
            Task { @MainActor in
                guard let self, self.socket === socket else { return }
                self.voice.configureConnection(sessionID: self.conversationID, connected: false)
            }
        }
    }

    // Exact-owner events only. Fixtures inject ownership; production uses BrainRuntime.
    func observeRuntimeHealth(_ event: NativeRuntimeHealthEvent) {
        guard !shuttingDown, (injectedRuntimeOwner?() ?? runtime.ownership) == event.ownership else { return }
        runtimeHealthWarning = event.healthWarning
        if event.phase == .unavailable || event.phase == .limited || (event.phase == .ready && !event.availableForActions) {
            serviceReachable = event.serviceReachable
            archiveRichTurn()
            if let id = streamMessageID, let index = messages.firstIndex(where: { $0.id == id }) {
                messages[index].metadata["responseIncomplete"] = true
                messages[index].metadata["deliveryError"] = event.reason
            }
            finishResponse()
            receiveTask?.cancel(); socket?.cancel(with: .goingAway, reason: nil); socket = nil; socketGeneration = UUID()
            codingPoll?.cancel(); codingPoll = nil; codingBusy = false
            if !coding.status.isEmpty { coding.status = "connection_lost" }
            voice.configureConnection(sessionID: conversationID, connected: false)
            richChat.configure(sessionID: nil, connectionID: nil)
            runtimeRevision = UUID(); conversationRevision = UUID(); switchingConversation = false
            recovery.configure(baseURL: nil, connectionID: nil)
            ready = false; appSessionScope = UUID()
            startupStatus = event.reason
            recoveryStatus = "Connection unavailable. Saved and visible messages retained; earlier actions are not retried automatically."
            if event.phase == .unavailable { error = event.reason; chatError = event.reason }
            return
        }
        if event.phase == .ready, event.availableForActions {
            serviceReachable = true
            if !event.reconnectVerifiedSession, !ready, !connecting {
                Task { [weak self] in await self?.start() }
                return
            }
        }
        if event.phase == .ready, event.availableForActions, event.reconnectVerifiedSession {
            let owner = event.ownership, previousID = conversationID
            ready = true; error = nil; chatError = nil; switchingConversation = true; startupStatus = "Reconnecting the verified local agent…"
            Task { [weak self] in
                guard let self, !self.shuttingDown, (self.injectedRuntimeOwner?() ?? self.runtime.ownership) == owner, self.conversationID == previousID else { return }
                let revision = self.conversationRevision, runtimeOwner = self.runtimeRevision
                self.switchingConversation = true
                defer { if self.conversationRevision == revision { self.switchingConversation = false } }
                self.recovery.configure(baseURL: owner.baseURL, connectionID: runtimeOwner)
                do {
                    _ = try await self.recovery.resolvePrimary()
                    guard self.ready, self.conversationRevision == revision, self.runtimeRevision == runtimeOwner,
                          (self.injectedRuntimeOwner?() ?? self.runtime.ownership) == owner else { return }
                    self.rememberCurrentSelection()
                    await self.recoverCurrentTranscript(revision: revision, runtimeOwner: runtimeOwner)
                    guard self.ready, self.conversationRevision == revision, self.runtimeRevision == runtimeOwner else { return }
                    await self.connectChat()
                    self.startupStatus = "Local agent connected after wake. Previous actions were not replayed."
                } catch {
                    guard self.conversationRevision == revision, self.runtimeRevision == runtimeOwner else { return }
                    self.recoveryStatus = "Wake transcript recovery unavailable. Visible messages retained; previous actions were not replayed."
                    self.error = "Session identity could not be recovered after wake. Restart explicitly before sending more requests."
                    self.ready = false
                }
            }
        }
    }

    private func request(_ path: String, body: [String: Any]? = nil, allowMissingConversation: Bool = false) async throws -> Any {
        guard ready else { throw NativeFailure("The local agent is not ready yet.") }
        let owner = runtimeRevision, origin = runtime.baseURL
        var req = URLRequest(url: URL(string: path, relativeTo: origin)!)
        if let body {
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await session.data(for: req)
        guard ready, owner == runtimeRevision, origin == runtime.baseURL else { throw NativeFailure("The local connection changed during this request. Earlier actions may have taken effect; inspect before retrying.") }
        let value = try JSONSerialization.jsonObject(with: data)
        let dict = value as? [String: Any] ?? [:]
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw NativeFailure(dict["detail"] as? String ?? "The local agent could not complete that request.")
        }
        if let issue = dict["error"] as? String, dict["status"] as? String != "failed", !(allowMissingConversation && issue == "Not found") { throw NativeFailure(issue) }
        return value
    }

    func start() async {
        guard !shuttingDown && !connecting && !ready else { return }
        let priorID = conversationID, priorRecords = messages.map(\.savedRecord)
        connecting = true; busy = true; error = nil
        defer { connecting = false; busy = false }
        do {
            try await runtime.start { self.startupStatus = $0 }
            guard !shuttingDown else { await runtime.stop(); return }
            serviceReachable = runtime.serviceReachable
            guard runtime.isReady else {
                ready = false
                if !serviceReachable { error = "The owned local service was not verified." }
                return
            }
            ready = true
            if let config = try await request("/api/llm/config") as? [String: Any] {
                endpoint = config["base_url"] as? String ?? ""
                if endpoint.isEmpty { endpoint = "http://127.0.0.1:11434" }
                modelName = config["model"] as? String ?? ""
            }
            try await resolveStartupConversation()
            if priorID == conversationID, !priorRecords.isEmpty {
                // Preserve unsaved visible/incomplete output across an explicit restart.
                var records = messages.map(\.savedRecord)
                for old in priorRecords {
                    if let id = old["id"] as? String, let index = records.firstIndex(where: { $0["id"] as? String == id }) { records[index] = old }
                    else { records.append(old) }
                }
                messages = records.map(NativeMessage.restored)
                await recoverCurrentTranscript(revision: conversationRevision, runtimeOwner: runtimeRevision)
            }
            await connectChat()
            await refreshCoding()
        } catch { self.error = error.localizedDescription; ready = false; serviceReachable = false; startupStatus = "The local agent could not start"; await runtime.stop() }
    }

    // Callable without starting BrainRuntime so the exact boot contract is testable.
    // UI records never implicitly become model context for an isolated session.
    func resolveStartupConversation() async throws {
        guard ready, !shuttingDown else { throw NativeFailure("The local agent is not ready for recovery.") }
        runtimeRevision = UUID(); conversationRevision = UUID()
        let runtimeOwner = runtimeRevision, revision = conversationRevision
        switchingConversation = true
        defer { if revision == conversationRevision { switchingConversation = false } }
        recovery.configure(baseURL: runtime.baseURL, connectionID: runtimeOwner)
        recoveryStatus = "Verifying the shared primary conversation…"
        let primary: String
        do { primary = try await recovery.resolvePrimary() }
        catch { if runtimeOwner == runtimeRevision { recoveryStatus = "Shared primary session unavailable. No recent saved thread was silently treated as shared." }; throw error }
        guard ready, runtimeOwner == runtimeRevision, revision == conversationRevision else { throw NativeFailure("The conversation changed during startup recovery.") }
        let remembered = try recovery.selectedConversationID()
        var selected: [String: Any]?
        if let remembered, !deletedConversationIDs.contains(remembered) { selected = try await readExactConversation(remembered) }
        guard ready, runtimeOwner == runtimeRevision, revision == conversationRevision else { throw NativeFailure("The conversation changed during selection recovery.") }
        if selected == nil {
            selected = try await readExactConversation(primary)
            guard ready, runtimeOwner == runtimeRevision, revision == conversationRevision else { throw NativeFailure("The primary selection changed during recovery.") }
            if selected == nil {
                // Only exact Not found permits requesting atomic insert-if-missing.
                // A concurrently created winning record is preserved by the backend.
                guard !deletedConversationIDs.contains(primary),
                      let receipt = try await request("/api/conversations/new", body: ["id": primary, "title": "Shared conversation", "create_if_missing": true]) as? [String: Any],
                      NativeConversationWire.bool(receipt["ok"]) == true, receipt["id"] as? String == primary,
                      NativeConversationWire.bool(receipt["create_if_missing"]) == true,
                      NativeConversationWire.bool(receipt["created"]) != nil,
                      let winning = receipt["conversation"] as? [String: Any], winning["id"] as? String == primary,
                      let winningRows = winning["messages"] as? [[String: Any]], winningRows.count <= 5_000,
                      JSONSerialization.isValidJSONObject(winningRows),
                      let winningData = try? JSONSerialization.data(withJSONObject: winningRows), winningData.count <= 8_388_608 else {
                    throw NativeFailure("Canonical shared conversation creation was not confirmed. Refresh before retrying.")
                }
                guard ready, runtimeOwner == runtimeRevision, revision == conversationRevision else { throw NativeFailure("The primary selection changed after creation; refresh before continuing.") }
                selected = try await readExactConversation(primary)
                guard selected != nil else { throw NativeFailure("Canonical shared conversation creation was acknowledged but readback was unavailable.") }
            }
        }
        guard let selected, ready, runtimeOwner == runtimeRevision, revision == conversationRevision else { throw NativeFailure("The recovered selection was unavailable or changed.") }
        try restoreThread(selected)
        try recovery.rememberSelection(conversationID)
        await recoverCurrentTranscript(revision: revision, runtimeOwner: runtimeOwner)
    }
    private func readExactConversation(_ id: String) async throws -> [String: Any]? {
        guard NativeSessionRecoveryWire.validID(id), !deletedConversationIDs.contains(id) else { throw NativeFailure("The selected conversation ID is unavailable.") }
        guard let body = try await request("/api/conversations/" + NativeSessionRecoveryWire.segment(id), allowMissingConversation: true) as? [String: Any] else { throw NativeFailure("The saved conversation was unreadable.") }
        if body["error"] as? String == "Not found" {
            guard body.count == 1 else { throw NativeFailure("The missing conversation response was ambiguous; no record was created.") }
            return nil
        }
        guard body["id"] as? String == id, let rows = body["messages"] as? [[String: Any]], rows.count <= 5_000,
              JSONSerialization.isValidJSONObject(rows), let data = try? JSONSerialization.data(withJSONObject: rows), data.count <= 8_388_608 else { throw NativeFailure("The saved conversation did not match the selected ID or exceeded recovery bounds.") }
        return body
    }
    private func rememberCurrentSelection() {
        do { try recovery.rememberSelection(conversationID) }
        catch { recoveryStatus = "Selection was not saved because the primary installation is unverified." }
    }
    private func recoverCurrentTranscript(revision: UUID, runtimeOwner: UUID) async {
        let id = conversationID
        guard let primary = recovery.primarySessionID else { recoveryStatus = "Shared session unavailable. Saved thread retained; runtime context was not restored."; return }
        do {
            let transcript = try await recovery.transcript(runtimeSessionID: id)
            guard ready, revision == conversationRevision, runtimeOwner == runtimeRevision, id == conversationID else { return }
            let records = try NativeSessionRecoveryWire.merge(existing: messages.map(\.savedRecord), transcript: transcript)
            messages = records.map(NativeMessage.restored)
            recoveryStatus = id == primary ? "Shared conversation connected. Available missed messages recovered; saved history remains. Snapshot-restored history is model context and is omitted by this recovery endpoint." : "Separate conversation connected. Available missed messages recovered for this thread. Saved history was not injected into model context; shared conversation context is not inherited automatically."
        } catch {
            guard revision == conversationRevision, runtimeOwner == runtimeRevision, id == conversationID else { return }
            recoveryStatus = "Transcript recovery unavailable. Saved messages retained; no claim that all missed turns were recovered."
        }
    }

    private func connectChat() async {
        guard ready, !shuttingDown else { return }
        voice.configureConnection(sessionID: conversationID, connected: false)
        receiveTask?.cancel(); socket?.cancel(with: .goingAway, reason: nil)
        socketGeneration = UUID()
        let generation = socketGeneration
        richChat.configure(sessionID: conversationID, connectionID: generation)
        var components = URLComponents(url: runtime.baseURL, resolvingAgainstBaseURL: false)!
        components.scheme = "ws"; components.path = "/v1/session"
        components.queryItems = [URLQueryItem(name: "session_id", value: conversationID)]
        let ws = session.webSocketTask(with: components.url!)
        socket = ws; ws.resume()
        receiveTask = Task { [weak self] in
            while !Task.isCancelled {
                do {
                    let incoming = try await ws.receive()
                    let data: Data
                    switch incoming {
                    case .string(let string): data = Data(string.utf8)
                    case .data(let bytes): data = bytes
                    @unknown default: continue
                    }
                    guard let frame = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
                    guard let self, !Task.isCancelled, self.socketGeneration == generation else { break }
                    if let sid = frame["session_id"] as? String, sid != self.conversationID { continue }
                    await self.consume(frame)
                } catch {
                    if !Task.isCancelled, self?.socketGeneration == generation {
                        self?.voice.configureConnection(sessionID: self?.conversationID, connected: false)
                        self?.richChat.configure(sessionID: nil, connectionID: nil)
                        if let self, let id = self.streamMessageID, let index = self.messages.firstIndex(where: { $0.id == id }) {
                            self.messages[index].metadata["responseIncomplete"] = true
                            self.messages[index].metadata["deliveryError"] = "Chat disconnected before this reply completed."
                        }
                        self?.socket = nil
                        self?.recordChatFailure("Chat disconnected. Retry to reconnect.")
                        self?.finishResponse()
                    }
                    break
                }
            }
        }
    }

    func consume(_ frame: [String: Any]) async {
        guard ready, !shuttingDown else { return }
        let owner = runtimeRevision, connection = socketGeneration
        let type = frame["type"] as? String ?? ""
        let payload = frame["payload"] as? [String: Any] ?? [:]
        let globalBudget = type == "state_push" && frame["event"] as? String == "cost_cap_hit"
        if let sid = frame["session_id"] as? String, sid != conversationID, !globalBudget { return }
        if let sid = payload["session_id"] as? String, sid != conversationID, !globalBudget { return }
        await voice.handle(frame: frame)
        guard ready, !shuttingDown, owner == runtimeRevision, connection == socketGeneration else { return }
        let structured = richChat.consume(frame, connectionID: socketGeneration)
        archiveRichTurn()
        if structured {
            if type == "refusal" || type == "budget_exceeded" {
                recordChatFailure(type == "refusal" ? (payload["reason"] as? String ?? "The agent declined this request.") : "The agent reported a budget limit for this request. Review the budget notice before retrying.")
                if let id = streamMessageID, let index = messages.firstIndex(where: { $0.id == id }) {
                    messages[index].metadata["responseIncomplete"] = true
                    messages[index].metadata["deliveryError"] = chatError ?? "Response incomplete"
                }
                finishResponse(); await persistConversation()
            }
            return
        }
        if type == "todo_update" {
            if payload["session_id"] as? String == conversationID, let rows = payload["todos"] as? [[String: Any]] { observedTodos = rows }
        } else if type == "stream_delta" {
            guard payload["kind"] as? String != "reasoning" else { return }
            let delta = payload["delta"] as? String ?? ""
            if !delta.isEmpty {
                if streamMessageID == nil { let id = UUID().uuidString; streamMessageID = id; messages.append(NativeMessage(id: id, role: "assistant", text: "")) }
                if let index = messages.firstIndex(where: { $0.id == streamMessageID }) { messages[index].text += delta }
            }
            if payload["is_final"] as? Bool == true {
                if streamMessageID == nil { recordChatFailure("The model returned an empty reply. Choose a different model or retry.") }
                archiveRichTurn()
                archiveResponsePayload(payload)
                finishResponse(); await persistConversation()
            }
        } else if type == "text_response" || type == "chat_response" {
            let text = payload["text"] as? String ?? ""
            if !text.isEmpty {
                if let index = messages.firstIndex(where: { $0.id == streamMessageID }) { messages[index].text = text }
                else { let id = UUID().uuidString; streamMessageID = id; messages.append(NativeMessage(id: id, role: "assistant", text: text)) }
            }
            archiveResponsePayload(payload)
            archiveRichTurn()
            if text.isEmpty && streamMessageID == nil { recordChatFailure("The model returned an empty reply. Choose a different model or retry.") }
            finishResponse(); await persistConversation()
        } else if type == "error" {
            recordChatFailure(payload["message"] as? String ?? payload["text"] as? String ?? "The agent could not generate a reply. Try a different model.")
            discardPartialResponse()
            finishResponse()
            await persistConversation()
        }
    }

    private func archiveRichTurn() {
        guard let id = streamMessageID, let index = messages.firstIndex(where: { $0.id == id }) else { return }
        for (key, value) in richChat.turnMetadata { messages[index].metadata[key] = value }
    }

    private func archiveResponsePayload(_ payload: [String: Any]) {
        guard let id = streamMessageID, let index = messages.firstIndex(where: { $0.id == id }) else { return }
        messages[index].metadata["response_payload"] = payload
        for key in ["model", "usage"] { if let value = payload[key] { messages[index].metadata[key] = value } }
    }

    func respondToChatPermission(_ response: NativeRichPermissionResponse) async throws {
        guard ready, !switchingConversation, response.sessionID == conversationID,
              response.connectionID == socketGeneration, let socket, socket.state == .running,
              richChat.permissions.contains(where: { $0.id == response.requestID && $0.session == conversationID && $0.supported && !$0.expired && $0.state == "responding" }) else {
            throw NativeFailure("The permission request or connection changed. Reconnect and request access again.")
        }
        let data = try JSONSerialization.data(withJSONObject: response.wireFrame)
        try await socket.send(.string(String(decoding: data, as: UTF8.self)))
    }

    private func finishResponse() { isSending = false; streamMessageID = nil; pendingUserMessageID = nil; responseDeadline?.cancel(); responseDeadline = nil }
    private func recordChatFailure(_ message: String) {
        error = message; chatError = message
        if let index = messages.lastIndex(where: { $0.id == pendingUserMessageID || (pendingUserMessageID == nil && $0.role == "user") }) { messages[index].metadata["deliveryError"] = message }
    }
    private func discardPartialResponse() { if let id = streamMessageID { messages.removeAll { $0.id == id } } }

    func uploadAttachments(_ urls: [URL]) async {
        guard ready, !uploadingAttachments, !isSending, !switchingConversation else { return }
        uploadingAttachments = true; attachmentError = nil
        let revision = conversationRevision
        defer { uploadingAttachments = false }
        for url in urls {
            do {
                let receipt = try await NativeAttachmentWire.upload(url, baseURL: runtime.baseURL, session: session)
                guard ready, conversationRevision == revision else { return }
                if !pendingAttachments.contains(where: { $0.id == receipt.id }) { pendingAttachments.append(receipt) }
            } catch { if conversationRevision == revision { attachmentError = "\(url.lastPathComponent): \(error.localizedDescription)" } }
        }
    }
    @discardableResult func sendChat(_ text: String, authorizedAttachmentIDs: [String]? = nil) async -> Bool {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard ready && !shuttingDown && !isSending && !switchingConversation && !uploadingAttachments && !text.isEmpty else { return false }
        if !pendingAttachments.isEmpty && authorizedAttachmentIDs != pendingAttachments.map(\.id) {
            attachmentError = "Review the current attachments before sending their contents to the configured model and fallbacks."
            return false
        }
        archiveRichTurn(); richChat.clearTurnPresentation()
        error = nil; chatError = nil; isSending = true; streamMessageID = nil
        if socket?.state != .running { await connectChat() }
        let userID = UUID().uuidString
        pendingUserMessageID = userID
        let attachments = pendingAttachments
        var record: [String: Any] = ["id": userID, "role": "user", "text": text, "content": text]
        if !attachments.isEmpty { record["attachments"] = attachments.map(\.record) }
        messages.append(NativeMessage(id: userID, role: "user", text: text, metadata: record))
        do {
            var payload: [String: Any] = ["text": text, "context": attachments.isEmpty ? [:] : ["attachment_content_authorized": true]]
            if !attachments.isEmpty { payload["attachments"] = attachments.map(\.record) }
            let data = try JSONSerialization.data(withJSONObject: ["type": "text_command", "hop": "client", "session_id": conversationID, "payload": payload])
            guard let socket else { throw NativeFailure("Chat is disconnected.") }
            try await socket.send(.string(String(decoding: data, as: UTF8.self)))
            let sentIDs = Set(attachments.map(\.id)); pendingAttachments.removeAll { sentIDs.contains($0.id) }
            await persistConversation()
            responseDeadline = Task { [weak self] in
                try? await Task.sleep(nanoseconds: 240_000_000_000)
                guard !Task.isCancelled, let self, self.isSending else { return }
                self.chatError = "The model has not replied within four minutes. You can stop this request and choose another model."
            }
            return true
        } catch {
            messages.removeAll { $0.id == userID }; finishResponse(); self.error = "Could not send: \(error.localizedDescription)"
            return false
        }
    }

    func stopChat() async { receiveTask?.cancel(); socket?.cancel(with: .goingAway, reason: nil); discardPartialResponse(); finishResponse(); await persistConversation(); await connectChat() }
    func newConversation() {
        guard ready && !shuttingDown && !isSending && !switchingConversation && !uploadingAttachments else { return }
        switchingConversation = true
        conversationRevision = UUID()
        let revision = conversationRevision
        Task {
            defer { if conversationRevision == revision { switchingConversation = false } }
            do {
                let result = try await request("/api/conversations/new", body: [:]) as? [String: Any] ?? [:]
                guard conversationRevision == revision else { return }
                guard let id = result["id"] as? String, !id.isEmpty else { throw NativeFailure("The agent did not confirm the new conversation.") }
                conversationID = id
                rememberCurrentSelection()
                recoveryStatus = "New isolated conversation. This exact session does not inherit shared-primary history."
                messages = []; pendingAttachments = []; attachmentError = nil; chatError = nil; await connectChat()
            } catch { self.error = error.localizedDescription }
        }
    }

    func openConversation(_ id: String) async {
        guard ready, !shuttingDown, !isSending, !switchingConversation, !uploadingAttachments, !deletedConversationIDs.contains(id) else {
            error = "Wait for attachment uploads or stop the current reply before switching conversations."; return
        }
        switchingConversation = true; conversationRevision = UUID()
        let revision = conversationRevision
        defer { if conversationRevision == revision { switchingConversation = false } }
        await conversationSaveTask?.value
        guard ready, revision == conversationRevision else { return }
        do {
            let segment = NativeMemoryWire.segment(id)
            guard let thread = try await request("/api/conversations/" + segment) as? [String: Any],
                  thread["id"] as? String == id,
                  let rows = thread["messages"] as? [[String: Any]] else {
                throw NativeFailure("The agent did not return the selected conversation.")
            }
            guard revision == conversationRevision, !deletedConversationIDs.contains(id) else { return }
            try restoreThread(["id": id, "messages": rows]); pendingAttachments = []; attachmentError = nil; chatError = nil; error = nil
            rememberCurrentSelection()
            await recoverCurrentTranscript(revision: revision, runtimeOwner: runtimeRevision)
            guard revision == conversationRevision, conversationID == id else { return }
            await connectChat()
        } catch { if revision == conversationRevision { self.error = error.localizedDescription } }
    }

    func conversationDeleted(_ id: String) {
        deletedConversationIDs.insert(id)
        guard conversationID == id else { return }
        conversationRevision = UUID(); socketGeneration = UUID()
        receiveTask?.cancel(); socket?.cancel(with: .goingAway, reason: nil)
        discardPartialResponse(); finishResponse(); messages = []; conversationID = ""; switchingConversation = false
        newConversation()
    }

    func refreshProviderConfiguration() async {
        do {
            guard let config = try await request("/api/llm/config") as? [String: Any] else { throw NativeFailure("Provider configuration is unreadable.") }
            endpoint = config["base_url"] as? String ?? ""; modelName = config["model"] as? String ?? ""
        } catch { self.error = error.localizedDescription }
    }
    func applySnapshotHistory(_ id: String, history: [[String: Any]]) async throws {
        guard ready, !chatMutationBusy, !id.isEmpty, id.count <= 256,
              !deletedConversationIDs.contains(id), history.count <= 500,
              JSONSerialization.isValidJSONObject(history),
              history.allSatisfy({ ($0["role"] as? String)?.isEmpty == false }) else {
            throw NativeFailure("Stop active chat or voice and finish uploads before applying a valid saved point.")
        }
        applyingSnapshotHistory = true
        switchingConversation = true; conversationRevision = UUID()
        let revision = conversationRevision
        defer { applyingSnapshotHistory = false; if conversationRevision == revision { switchingConversation = false } }
        await conversationSaveTask?.value
        guard ready, revision == conversationRevision else { throw NativeFailure("The conversation changed while finishing its previous save.") }
        // The feature reviewed runtime restoration separately. This operation
        // also replaces the saved UI thread, preserving every history field.
        guard let receipt = try await request("/api/conversations/save", body: ["id": id, "messages": history]) as? [String: Any],
              receipt["id"] as? String == id else {
            throw NativeFailure("The saved Chat thread replacement was not confirmed. Runtime restoration is a separate operation.")
        }
        guard ready, revision == conversationRevision else { throw NativeFailure("The connection changed after requesting the thread save. Refresh before continuing.") }
        guard let stored = try await request("/api/conversations/" + NativeMemoryWire.segment(id)) as? [String: Any],
              stored["id"] as? String == id, let rows = stored["messages"] as? [[String: Any]],
              try JSONSerialization.data(withJSONObject: rows, options: [.sortedKeys]) == JSONSerialization.data(withJSONObject: history, options: [.sortedKeys]) else {
            throw NativeFailure("Thread save was acknowledged, but readback differs. The visible conversation was not replaced; refresh the saved thread.")
        }
        guard ready, revision == conversationRevision else { throw NativeFailure("The connection changed during readback. Refresh the saved thread.") }
        try restoreThread(stored)
        rememberCurrentSelection()
        pendingAttachments = []; attachmentError = nil; chatError = nil; error = nil
        await connectChat()
    }
    private func persistConversation() async {
        let id = conversationID
        guard !id.isEmpty, !deletedConversationIDs.contains(id) else { return }
        let rows = messages.map(\.savedRecord), owner = runtimeRevision, previous = conversationSaveTask
        let task = Task { [weak self] in
            await previous?.value
            guard let self, self.ready, owner == self.runtimeRevision, !self.deletedConversationIDs.contains(id) else { return }
            do {
                guard let receipt = try await self.request("/api/conversations/save", body: ["id": id, "messages": rows]) as? [String: Any], receipt["id"] as? String == id else { throw NativeFailure("The saved conversation acknowledgement did not match its ID.") }
            } catch { if owner == self.runtimeRevision, id == self.conversationID { self.error = "Conversation could not be saved: \(error.localizedDescription)" } }
        }
        conversationSaveTask = task
        await task.value
    }

    func saveSettings() async {
        guard !modelName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { error = "Choose an installed model first."; return }
        busy = true; error = nil; defer { busy = false }
        do {
            let value = try await request("/api/llm/config", body: ["provider": "ollama", "model": modelName, "base_url": endpoint, "fallback_providers": []]) as? [String: Any] ?? [:]
            let configured = value["reconfigured"] as? [String: Any] ?? [:]
            guard configured["ok"] as? Bool == true, configured["available"] as? Bool == true else { throw NativeFailure("Settings were saved, but that model is not available. Start Ollama and check the endpoint and installed model name.") }
            saveProfile()
        }
        catch { self.error = error.localizedDescription }
    }
    func finishOnboarding() async {
        await saveSettings()
        if error == nil { onboarded = true; prefs.set(true, forKey: "onboarded"); showProviderSetup = true }
    }
    func completeProfileOnboarding() {
        saveProfile(); onboarded = true; prefs.set(true, forKey: "onboarded"); showProviderSetup = true
    }
    func openProviderSetup() { showProviderSetup = true }
    func dismissProviderSetup() { showProviderSetup = false }
    // Called only after the provider feature has verified its own HTTP receipt.
    // Clearing a local sheet must never mark backend setup complete.
    func completeProviderSetup() { showProviderSetup = false }
    func saveProfile() {
        prefs.set(displayName, forKey: "displayName"); prefs.set(avatarChoice, forKey: "avatarChoice"); prefs.set(importedAvatarPath, forKey: "importedAvatarPath")
    }
    func importAvatar(_ url: URL) {
        let scoped = url.startAccessingSecurityScopedResource(); defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        do {
            guard NSImage(contentsOf: url) != nil else { throw NativeFailure("Choose a supported image file.") }
            let base = ProcessInfo.processInfo.environment["FERAL_HOME"].map { URL(fileURLWithPath: $0) } ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".feral-native-preview")
            let folder = base.appendingPathComponent("avatars"); try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let dest = folder.appendingPathComponent(UUID().uuidString + "." + url.pathExtension)
            try FileManager.default.copyItem(at: url, to: dest)
            importedAvatarPath = dest.path; avatarChoice = "imported"; saveProfile()
        } catch { self.error = error.localizedDescription }
    }

    func refreshCoding() async {
        do {
            let value = try await request("/api/coding") as? [String: Any] ?? [:]
            coding.engineReady = (value["agents"] as? [[String: Any]] ?? []).contains { $0["agent_id"] as? String == "opencode" && $0["available"] as? Bool == true }
            if coding.workspace.isEmpty { coding.workspace = (value["workspaces"] as? [String] ?? []).first ?? "" }
            let provider = value["provider"] as? [String: Any] ?? [:]
            coding.prepared = provider["prepared"] as? Bool ?? false
            if let endpoint = provider["base_url"] as? String { coding.endpoint = endpoint }
            if let model = provider["source_model"] as? String { coding.modelName = model }
            if coding.endpoint.isEmpty { coding.endpoint = "http://127.0.0.1:11434/v1" }
        } catch { self.error = error.localizedDescription }
    }
    func stageCodingSession(_ handle: String, workspace: String) async -> Bool {
        guard ready, !codingBusy, !["running", "awaiting_permission"].contains(coding.status), !handle.isEmpty, !workspace.isEmpty else {
            error = "Stop the current coding task before opening another session."; return false
        }
        codingPoll?.cancel(); codingHandle = handle; coding.workspace = workspace
        coding.status = "recalled"; coding.text = "Session selected. Send a new task to resume it; the agent will recheck this project's authorization."
        coding.actions = []; coding.permissions = []; error = nil
        await refreshCoding()
        return error == nil
    }
    func grantWorkspace(_ path: String) async {
        codingBusy = true; error = nil; defer { codingBusy = false }
        do {
            let value = try await request("/api/coding/workspaces", body: ["path": path]) as? [String: Any] ?? [:]
            let granted = value["path"] as? String ?? path
            if granted != coding.workspace { codingHandle = ""; coding.status = ""; coding.text = ""; coding.actions = []; coding.permissions = [] }
            coding.workspace = granted
        }
        catch { self.error = error.localizedDescription }
    }
    @discardableResult func prepareCoding() async -> Bool {
        guard ready, !codingBusy, !["running", "awaiting_permission"].contains(coding.status) else { return false }
        codingBusy = true; error = nil; defer { codingBusy = false }
        do { _ = try await request("/api/coding/provider", body: ["base_url": coding.endpoint, "model": coding.modelName, "prepare": true]); codingHandle = ""; await refreshCoding() }
        catch { self.error = error.localizedDescription }
        return error == nil && coding.prepared
    }
    func startCoding(_ prompt: String) async {
        guard !codingBusy && !["running", "awaiting_permission"].contains(coding.status) else { return }
        codingBusy = true; error = nil; defer { codingBusy = false }
        do {
            var body: [String: Any] = ["workspace_dir": coding.workspace, "prompt": prompt]
            if !codingHandle.isEmpty { body["session_handle"] = codingHandle }
            let value = try await request("/api/coding/tasks", body: body) as? [String: Any] ?? [:]
            updateCoding(value)
            codingPoll?.cancel()
            codingPoll = Task { [weak self] in
                while !Task.isCancelled, let self, ["running", "awaiting_permission"].contains(self.coding.status) {
                    try? await Task.sleep(nanoseconds: 800_000_000)
                    guard !Task.isCancelled else { break }
                    do { let result = try await self.request("/api/coding/sessions/\(self.codingHandle)") as? [String: Any] ?? [:]; self.updateCoding(result) }
                    catch { self.error = error.localizedDescription; break }
                }
            }
        } catch { self.error = error.localizedDescription }
    }
    func updateCoding(_ value: [String: Any]) {
        codingHandle = value["session_handle"] as? String ?? codingHandle
        coding.status = value["status"] as? String ?? coding.status
        if coding.status == "failed" { error = value["error"] as? String ?? "The coding task failed." }
        coding.text = value["text"] as? String ?? ""
        var actionOrder: [String] = []
        var actionValues: [String: [String: Any]] = [:]
        for (i, item) in (value["tool_calls"] as? [[String: Any]] ?? []).enumerated() {
            let id = item["tool_call_id"] as? String ?? "\(i)"
            if actionValues[id] == nil { actionOrder.append(id) }
            var merged = actionValues[id] ?? [:]
            for (key, val) in item { if let str = val as? String, str.isEmpty { continue }; merged[key] = val }
            actionValues[id] = merged
        }
        coding.actions = actionOrder.map { id in let item = actionValues[id]!; return NativeAction(id: id, title: item["title"] as? String ?? item["tool_name"] as? String ?? "Tool action", status: item["status"] as? String ?? "", detail: pretty(item)) }
        coding.permissions = (value["pending_permissions"] as? [[String: Any]] ?? []).map { item in
            let details = item["details"] as? [String: Any] ?? [:]
            let input = details["rawInput"] as? [String: Any] ?? [:]
            let diffs = (details["content"] as? [[String: Any]] ?? []).filter { $0["type"] as? String == "diff" }
            let before = diffs.map { (($0["path"] as? String ?? "File") + "\n" + ($0["oldText"] as? String ?? "(empty file)")) }.joined(separator: "\n\n")
            let after = diffs.map { (($0["path"] as? String ?? "File") + "\n" + ($0["newText"] as? String ?? "")) }.joined(separator: "\n\n")
            func concrete(_ key: String) -> Bool { !(input[key] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
            let kind = details["kind"] as? String ?? ""
            let concreteDiff = diffs.contains { !($0["path"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && $0["newText"] is String }
            let target = concrete("filePath") || concrete("path") || (details["locations"] as? [[String: Any]] ?? []).contains {
                !($0["path"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            }
            let reviewable: Bool
            switch kind {
            case "execute": reviewable = concrete("command")
            case "fetch": reviewable = concrete("url")
            case "read", "delete": reviewable = target
            case "edit": reviewable = concreteDiff || (target && (concrete("diff") || input["content"] is String))
            default: reviewable = false
            }
            return NativePermission(id: item["request_id"] as? String ?? "", title: item["title"] as? String ?? details["title"] as? String ?? "Review this action", before: before, after: after, diff: input["diff"] as? String ?? input["command"] as? String ?? pretty(details), reviewable: reviewable)
        }
    }
    func answerPermission(_ id: String, _ allow: Bool) async {
        guard !id.isEmpty else { error = "The approval request is invalid."; return }
        guard let permission = coding.permissions.first(where: { $0.id == id }), !allow || permission.reviewable else {
            error = "The coding action has no reviewable command or target."; return
        }
        codingBusy = true; defer { codingBusy = false }
        do { let value = try await request("/api/coding/permissions/\(id)", body: ["decision": allow ? "allow_once" : "reject_once"]) as? [String: Any] ?? [:]; if value["status"] != nil { updateCoding(value) } }
        catch { self.error = error.localizedDescription }
    }
    func cancelCoding() async {
        guard ready, !codingBusy, !codingHandle.isEmpty else { return }
        let handle = codingHandle
        codingBusy = true; defer { codingBusy = false }
        do {
            guard let receipt = try await request("/api/coding/sessions/" + NativeMemoryWire.segment(handle) + "/cancel", body: [:]) as? [String: Any],
                  receipt["handle"] as? String == handle, NativeIntegrationWire.bool(receipt["closed"]) == true,
                  codingHandle == handle else {
                throw NativeFailure("Coding session closure was not confirmed. Inspect its actual status before retrying; the request may already have affected the runtime.")
            }
            codingPoll?.cancel(); coding.status = "closed"; coding.permissions = []; codingHandle = ""; error = nil
        } catch { self.error = error.localizedDescription }
    }
    func refreshMemory() async {
        do { memories = (try await request("/internal/memory/recent?limit=30") as? [[String: Any]] ?? []).enumerated().map { i, row in NativeMemory(id: String(describing: row["id"] ?? i), title: row["title"] as? String ?? "Memory", detail: row["text"] as? String ?? row["content"] as? String ?? pretty(row)) } }
        catch { self.error = error.localizedDescription }
    }
    func refreshDevices() async {
        do { let value = try await request("/api/devices/connected") as? [String: Any] ?? [:]; devices = (value["devices"] as? [[String: Any]] ?? []).enumerated().map { i, row in NativeDevice(id: String(describing: row["node_id"] ?? row["id"] ?? i), title: row["name"] as? String ?? row["device_name"] as? String ?? "Connected device", detail: pretty(row)) } }
        catch { self.error = error.localizedDescription }
    }
    // Snapshot visible partial output honestly before terminating the owned backend.
    func flushConversationForShutdown() async {
        shuttingDown = true
        receiveTask?.cancel(); socket?.cancel(with: .goingAway, reason: nil)
        archiveRichTurn()
        if let id = streamMessageID, let index = messages.firstIndex(where: { $0.id == id }) {
            messages[index].metadata["responseIncomplete"] = true
            messages[index].metadata["deliveryError"] = "The app closed before this reply completed."
        }
        finishResponse()
        await persistConversation()
    }
    func shutdown() async {
        if let workspaceCenter { for token in workspaceObservers { workspaceCenter.removeObserver(token) } }
        workspaceObservers = []; workspaceCenter = nil
        await flushConversationForShutdown()
        if voice.state != "off" { await voice.stop() }
        voice.configureConnection(sessionID: nil, connected: false)
        richChat.configure(sessionID: nil, connectionID: nil)
        responseDeadline?.cancel(); codingPoll?.cancel(); receiveTask?.cancel(); socket?.cancel(with: .goingAway, reason: nil)
        await runtime.stop(); runtimeRevision = UUID(); recovery.configure(baseURL: nil, connectionID: nil); session.invalidateAndCancel(); ready = false; serviceReachable = false
    }
}

private func pretty(_ value: Any) -> String { guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]) else { return String(describing: value) }; return String(decoding: data, as: UTF8.self) }
