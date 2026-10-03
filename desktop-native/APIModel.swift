import SwiftUI
import AppKit
import CoreFoundation

@MainActor final class NativeModel: ObservableObject {
    @Published var ready = false
    @Published private(set) var serviceReachable = false
    @Published var busy = false
    @Published var error: String?
    @Published var chatError: String?
    @Published private(set) var runtimeHealthWarning: String?
    @Published private(set) var profileArchivePaused = false
    @Published private(set) var profileArchiveError: String?
    private var profileArchiveStoppedOwner: NativeRuntimeOwnership?
    private var profileArchiveTask: Task<Void, Never>?
    @Published private(set) var recoveryStatus = "Shared conversation recovery has not been verified."
    @Published var startupStatus = "Starting your local agent…"
    @Published var isSending = false
    @Published private(set) var chatTurnStatus = "Connecting verified chat…"
    @Published private(set) var unresolvedChatRequest:NativeTurnReference?
    @Published private(set) var checkingChatStatus = false
    @Published private(set) var chatRecoveryBlocked = false
    @Published private(set) var contextStatus = "Verifying conversation context…"
    @Published private(set) var contextSetupPending = false
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
    private var capabilityDeadline:Task<Void,Never>?
    private var statusDeadline:Task<Void,Never>?
    private var contextDeadline: Task<Void, Never>?
    private var contextState = NativeContextCheckpointState()
    private var pendingContextCreationSID: String?
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
    private var conversationSaveTask: Task<Bool, Never>?
    private var workspaceCenter: NotificationCenter?
    private var workspaceObservers: [NSObjectProtocol] = []
    private let injectedRuntimeOwner: (() -> NativeRuntimeOwnership?)?
    private let injectedChatSender: (([String:Any]) async throws -> Void)?
    private var chatTurns = NativeChatTurnState()
    var trackedChatReference:NativeTurnReference? { chatTurns.active }
    var chatCapabilityFrame:[String:Any]? { chatTurns.capabilitiesFrame }
    var chatConnectionID:UUID { socketGeneration }
    var chatReceiptReady:Bool { chatTurns.ready }
    var chatCanSend:Bool { ready && chatTurns.ready && contextState.permitsSubmission && !contextSetupPending && !chatRecoveryBlocked && unresolvedChatRequest == nil && !isSending && !switchingConversation && !uploadingAttachments && !shuttingDown }
    var contextManaged: Bool { contextState.managed || contextRequired(conversationID) }
    var contextNeedsAttention: Bool { chatTurns.ready && !contextState.permitsSubmission }
    var contextReady: Bool { contextState.capability?.ready == true }
    var canCreateSavedContextChat: Bool { ready && chatTurns.ready && contextState.supportsCreation && !contextSetupPending && !chatMutationBusy }
    var contextUnsupportedOperations: Set<String> { contextManaged ? NativeContextCheckpointWire.unsupportedPaths : [] }
    var contextToolsMayMutate: Bool { !contextSetupPending && contextState.permitsSubmission }
    var contextCheckpoint: NativeContextCheckpoint? { contextState.capability?.checkpoint }
    var contextRecoveryReview: NativeContextRecoveryReview? {
        guard let fence = contextState.recoveryFence else { return nil }
        return NativeContextRecoveryReview(fence: fence, connectionID: socketGeneration, selectionID: conversationRevision)
    }
    var contextRecoverySelectionID: UUID { conversationRevision }
    var canRecoverSavedContext: Bool { ready && chatTurns.ready && contextState.canRecover && !chatMutationBusy && !contextSetupPending && !chatRecoveryBlocked && unresolvedChatRequest == nil }
    var canCheckSavedContextRecovery: Bool { ready && chatTurns.ready && contextState.canCheckRecovery && !chatMutationBusy && !contextSetupPending && !chatRecoveryBlocked && unresolvedChatRequest == nil }
    private var contextPreferences: NativeContextCheckpointPreferences { NativeContextCheckpointPreferences(defaults: prefs, recoveryJournalURL: contextRecoveryJournalURL) }
    private func contextRequired(_ id: String) -> Bool {
        !preferencesUnavailable && contextPreferences.required(id, primary: recovery.primarySessionID)
    }
    private func configureContext(sessionID: String?, connectionID: UUID?, required: Bool, requestID: String?) {
        contextState.configure(sessionID: sessionID, connectionID: connectionID, required: required, requestID: requestID)
        restorePendingContextRecovery()
    }
    private func restorePendingContextRecovery() {
        guard let sessionID = contextState.sessionID, let primary = recovery.primarySessionID else { return }
        do {
            if let pending = try contextPreferences.pendingRecovery(session: sessionID, primary: primary),
               contextState.trackedRecoveryFence != pending && contextState.recoveryReconciledFence != pending {
                contextState.restorePendingRecovery(pending)
            }
        } catch { contextState.blockRecoveryPreferences() }
    }
    func canDeleteConversation(_ id: String) -> Bool {
        !switchingConversation && id != conversationID && !contextRequired(id)
    }
    private func updateVoiceReadiness() {
        voice.configureConnection(sessionID: conversationID, connected: !effectsPaused && ready && chatTurns.ready && contextState.permitsSubmission && !contextManaged && !contextSetupPending)
    }

    private lazy var recovery = NativeSessionRecoveryModel(preferences: prefs, transport: { [weak self] request in
        guard let self, !self.effectsPaused else { throw NativeFailure("Local session recovery is paused while the profile stops.") }
        let (data, response) = try await self.session.data(for: request)
        guard !self.effectsPaused else { throw NativeFailure("The local profile stopped during session recovery.") }
        guard let http = response as? HTTPURLResponse else { throw NativeFailure("Local session recovery was unavailable.") }
        return (data, http)
    })
    private let prefs: UserDefaults
    private let preferenceSuiteName: String?
    private let contextRecoveryJournalURL: URL?
    private let preferencesUnavailable: Bool
    private let session: URLSession
    private let transportDelegate = NativeLocalSessionDelegate()
    lazy var voice = NativeVoiceEngine(sendFrame: { [weak self] frame in
        guard let self, !self.preferencesUnavailable, !self.effectsPaused, self.ready, !self.switchingConversation, !self.contextManaged, !self.contextSetupPending,
              self.contextState.permitsSubmission, let socket = self.socket, socket.state == .running,
              frame["session_id"] as? String == self.conversationID else { throw NativeFailure("Voice is disconnected or the conversation changed.") }
        let data = try JSONSerialization.data(withJSONObject: frame)
        try await socket.send(.string(String(decoding: data, as: UTF8.self)))
    })
    lazy var profileArchive = NativeProfileArchiveFeature(
        allowedDestinations: Set(NativeDestination.allCases.map(\.rawValue)),
        ownerStatus: { [weak self] owner in
            guard let self else { return .superseded }
            if self.runtime.isCurrent(owner) { return .current }
            return self.runtime.isQuiesced(owner) ? .quiesced : .superseded
        }, quiesce: { [weak self] owner in
            guard let self else { throw NativeProfileArchiveFailure.changed }
            try await self.quiesceForProfileArchive(owner)
        }, executor: { command in
            try await NativeProfileArchiveProcessExecutor.bundled().execute(command)
        })
    var canPrepareProfileArchive: Bool {
        !preferencesUnavailable && preferenceSuiteName != nil && ready && !busy && !connecting
            && !chatMutationBusy && !codingBusy && !contextSetupPending && contextState.canSaveDisplay
            && runtime.ownership != nil
            && !profileArchivePaused && !profileArchive.busy
    }
    func profileArchiveScope() throws -> NativeProfileArchiveScope {
        guard canPrepareProfileArchive, let owner = runtime.ownership,
              let config = runtime.profileConfigRoot, let data = runtime.profileDataRoot,
              let primary = recovery.primarySessionID, let suite = preferenceSuiteName else {
            throw NativeFailure("Connect the local agent and finish active chat, voice or coding work before reviewing a profile archive.")
        }
        return NativeProfileArchiveScope(configRoot: config, dataRoot: data, defaults: prefs,
            suiteName: suite, primarySessionID: primary, runtimeOwner: owner)
    }
    private func quiesceForProfileArchive(_ owner: NativeRuntimeOwnership) async throws {
        guard canPrepareProfileArchive || profileArchive.busy,
              !shuttingDown, runtime.isCurrent(owner), !chatMutationBusy, !codingBusy,
              !connecting, !busy, !contextSetupPending, contextState.canSaveDisplay else {
            throw NativeProfileArchiveFailure.changed
        }
        try NativeLocalActionGate.shared.pause(origin: owner.baseURL)
        profileArchivePaused = true
        voice.configureConnection(sessionID: nil, connected: false)
        recovery.configure(baseURL: nil, connectionID: nil)
        let saved = await flushConversationForShutdown()
        richChat.configure(sessionID: nil, connectionID: nil)
        responseDeadline?.cancel(); codingPoll?.cancel(); contextDeadline?.cancel()
        capabilityDeadline?.cancel(); statusDeadline?.cancel()
        do {
            try await runtime.stop(ifOwnedBy: owner)
        } catch {
            // The selected runtime must not silently resume after an uncertain stop.
            ready = false; serviceReachable = false
            startupStatus = "Profile shutdown was not confirmed. Inspect the local runtime before continuing."
            throw error
        }
        guard runtime.isQuiesced(owner) else { throw NativeProfileArchiveFailure.unconfirmed }
        profileArchiveStoppedOwner = owner
        runtimeRevision = UUID(); socketGeneration = UUID()
        recovery.configure(baseURL: nil, connectionID: nil)
        ready = false; serviceReachable = false; runtimeHealthWarning = nil
        startupStatus = "Your original profile is stopped for backup or restore."
        guard saved else {
            throw NativeFailure("The visible conversation could not be saved. The original profile is stopped; no archive was dispatched. Restart it and verify the conversation before trying again.")
        }
    }
    var canResumeOriginalProfile: Bool {
        guard let owner = profileArchiveStoppedOwner else { return false }
        return profileArchivePaused && runtime.isQuiesced(owner) && !profileArchive.busy
            && profileArchive.review == nil && profileArchive.applyReview == nil
            && profileArchive.phase != .unconfirmed
    }
    func resumeOriginalProfile() async {
        guard canResumeOriginalProfile else { return }
        profileArchiveStoppedOwner = nil; profileArchivePaused = false; shuttingDown = false
        await start()
    }
    func performProfileArchive(_ operation: @escaping @MainActor () async throws -> Void) {
        guard profileArchiveTask == nil else { return }
        profileArchiveError = nil
        profileArchiveTask = Task { [weak self] in
            guard let self else { return }
            defer { self.profileArchiveTask = nil }
            do { try await operation() }
            catch { self.profileArchiveError = error.localizedDescription }
        }
    }

    var effectsPaused: Bool { shuttingDown || profileArchivePaused }
    var localRuntimeGeneration: UUID { runtimeRevision }
    var featureBaseURL: URL? { !preferencesUnavailable && !effectsPaused && ready ? runtime.baseURL : nil }
    var securityBaseURL: URL? { !preferencesUnavailable && !effectsPaused && (serviceReachable || ready) ? runtime.baseURL : nil }
    var activeConversationID: String { conversationID }
    // App REST dispatch can run agent turns without Chat turn IDs. Give it
    // a distinct scope so late app replies cannot complete an active Chat turn.
    var appSurfaceSessionID: String? { conversationID.isEmpty ? nil : "native-apps-" + appSessionScope.uuidString }
    var chatMutationBusy: Bool { shuttingDown || isSending || switchingConversation || uploadingAttachments || contextState.recoveryRequestID != nil || !["off", "ended"].contains(voice.state) }
    // Applying the reviewed saved point owns its own operation lock. Its
    // thread transition must not invalidate that same review mid-callback.
    var chatToolsHostBusy: Bool { effectsPaused || isSending || (switchingConversation && !applyingSnapshotHistory) || uploadingAttachments || contextState.recoveryRequestID != nil || !["off", "ended"].contains(voice.state) }

    func restoreThread(_ thread: [String: Any]) throws {
        guard let id = thread["id"] as? String, !id.isEmpty,
              !deletedConversationIDs.contains(id),
              let rows = thread["messages"] as? [[String: Any]] else {
            throw NativeFailure("The agent returned an unreadable conversation.")
        }
        conversationID = id; messages = rows.map(NativeMessage.restored); observedTodos = nil
        richChat.configure(sessionID: id, connectionID: socketGeneration)
        chatTurns.configure(sessionID:nil,connectionID:nil);recoverSavedChatRequest()
        contextSetupPending = false; pendingContextCreationSID = nil
        configureContext(sessionID: id, connectionID: nil, required: contextRequired(id), requestID: nil)
        contextStatus = contextState.message
    }

    /// A named suite equal to the app domain can return nil on macOS.
    /// Standard defaults already owns that domain; never create/reset it.
    static func resolvePreferences(suiteName: String, bundleIdentifier: String?, standard: UserDefaults,
                                   factory: (String) -> UserDefaults? = { UserDefaults(suiteName: $0) }) -> UserDefaults? {
        if suiteName == bundleIdentifier { return standard }
        guard !suiteName.isEmpty else { return nil }
        return factory(suiteName)
    }

    init(session injectedSession: URLSession? = nil, preferences: UserDefaults? = nil, recoveryJournalURL: URL? = nil, runtimeOwner: (() -> NativeRuntimeOwnership?)? = nil,
         preferencesResolver: (() -> UserDefaults?)? = nil, chatSender: (([String:Any]) async throws -> Void)? = nil,
         preferenceSuite: String? = nil) {
        injectedRuntimeOwner = runtimeOwner
        injectedChatSender = chatSender
        preferenceSuiteName = preferenceSuite ?? ((preferences != nil || preferencesResolver != nil) ? nil :
            (ProcessInfo.processInfo.environment["FERAL_NATIVE_PREFS_SUITE"] ?? "ai.feral.native.preview"))
        if let recoveryJournalURL { contextRecoveryJournalURL = recoveryJournalURL }
        else if preferences != nil || preferencesResolver != nil { contextRecoveryJournalURL = nil }
        else {
            let home = ProcessInfo.processInfo.environment["FERAL_HOME"].map { URL(fileURLWithPath: $0) }
                ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".feral-native-preview")
            contextRecoveryJournalURL = home.appendingPathComponent("native-context-recovery.json")
        }
        let resolved: UserDefaults?
        if let preferences { resolved = preferences }
        else if let preferencesResolver { resolved = preferencesResolver() }
        else {
            resolved = Self.resolvePreferences(suiteName: ProcessInfo.processInfo.environment["FERAL_NATIVE_PREFS_SUITE"] ?? "ai.feral.native.preview",
                                               bundleIdentifier: Bundle.main.bundleIdentifier, standard: .standard)
        }
        preferencesUnavailable = resolved == nil
        // Failure never reads or writes this placeholder domain. It keeps the
        // recovery dependency initialized while startup/profile effects are fenced.
        prefs = resolved ?? .standard
        if let resolved {
            displayName = resolved.string(forKey: "displayName") ?? ""
            avatarChoice = resolved.string(forKey: "avatarChoice") ?? "photo"
            importedAvatarPath = resolved.string(forKey: "importedAvatarPath")
            onboarded = resolved.bool(forKey: "onboarded")
            showProviderSetup = !resolved.bool(forKey: "onboarded")
        } else {
            error = "Local preferences are unavailable. Your saved profile has not been changed. Quit and reopen FERAL; no reset is required."
            startupStatus = "Local preferences are unavailable. Startup is paused."
        }
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
                self.updateVoiceReadiness()
            }
        }
        transportDelegate.onSocketClose = { [weak self] socket in
            Task { @MainActor in
                guard let self, self.socket === socket else { return }
                self.voice.configureConnection(sessionID: self.conversationID, connected: false)
            }
        }
    }

    private func preferencesPermitEffects() -> Bool {
        guard preferencesUnavailable else { return true }
        error = "Local preferences are unavailable. Your saved profile has not been changed. Quit and reopen FERAL; no reset is required."
        startupStatus = "Local preferences are unavailable. Startup is paused."
        ready = false
        return false
    }

    // Exact-owner events only. Fixtures inject ownership; production uses BrainRuntime.
    func observeRuntimeHealth(_ event: NativeRuntimeHealthEvent) {
        guard preferencesPermitEffects() else { return }
        guard !shuttingDown, (injectedRuntimeOwner?() ?? runtime.ownership) == event.ownership else { return }
        runtimeHealthWarning = event.healthWarning
        if event.phase == .unavailable || event.phase == .limited || (event.phase == .ready && !event.availableForActions) {
            serviceReachable = event.serviceReachable
            archiveRichTurn()
            if let id = streamMessageID, let index = messages.firstIndex(where: { $0.id == id }) {
                messages[index].metadata["responseIncomplete"] = true
                messages[index].metadata["deliveryError"] = event.reason
            }
            if chatTurns.isTracked { markTrackedUnknown("The local agent became unavailable before the whole-turn receipt. Earlier effects are unknown; check status after reconnection.") } else { finishResponse() }
            receiveTask?.cancel(); socket?.cancel(with: .goingAway, reason: nil); socket = nil; socketGeneration = UUID()
            chatTurns.configure(sessionID:nil,connectionID:nil);checkingChatStatus = false
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

    private func request(_ path: String, body: [String: Any]? = nil, allowMissingConversation: Bool = false,
                         shutdownSave: Bool = false) async throws -> Any {
        guard preferencesPermitEffects() else { throw NativeFailure("Local preferences are unavailable; startup and profile changes are paused.") }
        let permitsFinalSave = shutdownSave && path == "/api/conversations/save" && body != nil
        guard !effectsPaused || permitsFinalSave else { throw NativeFailure("The local profile is stopping or stopped. Finish the archive review or restart the original profile before another action.") }
        guard ready else { throw NativeFailure("The local agent is not ready yet.") }
        let owner = runtimeRevision, origin = runtime.baseURL
        var req = URLRequest(url: URL(string: path, relativeTo: origin)!)
        if let body {
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await session.data(for: req)
        guard ready, !effectsPaused || permitsFinalSave, owner == runtimeRevision, origin == runtime.baseURL else { throw NativeFailure("The local connection changed during this request. Earlier actions may have taken effect; inspect before retrying.") }
        let value = try JSONSerialization.jsonObject(with: data)
        let dict = value as? [String: Any] ?? [:]
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw NativeFailure(dict["detail"] as? String ?? "The local agent could not complete that request.")
        }
        if let issue = dict["error"] as? String, dict["status"] as? String != "failed", !(allowMissingConversation && issue == "Not found") { throw NativeFailure(issue) }
        return value
    }

    func start() async {
        guard preferencesPermitEffects() else { return }
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
            try NativeLocalActionGate.shared.activate(origin: runtime.baseURL)
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
        guard preferencesPermitEffects() else { throw NativeFailure("Local preferences are unavailable; startup and profile changes are paused.") }
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
        if let remembered, contextPreferences.pending(primary: primary) == remembered {
            conversationID = remembered; messages = []; pendingContextCreationSID = remembered; contextSetupPending = true
            recoveryStatus = "Checking interrupted saved-context setup for this exact new chat. No task is being retried."
            await connectChat()
            return
        }
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
        guard preferencesPermitEffects() else { return }
        do { try recovery.rememberSelection(conversationID) }
        catch { recoveryStatus = "Selection was not saved because the primary installation is unverified." }
    }
    private func recoverCurrentTranscript(revision: UUID, runtimeOwner: UUID) async {
        guard preferencesPermitEffects() else { return }
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
        guard preferencesPermitEffects() else { return }
        guard ready, !shuttingDown else { return }
        if chatTurns.isTracked {
            let revision = conversationRevision
            markTrackedUnknown("The chat connection is changing before its terminal receipt arrived. Check status; the request has not been retried.")
            await persistConversation()
            guard ready,!shuttingDown,revision == conversationRevision else { return }
        }
        voice.configureConnection(sessionID: conversationID, connected: false)
        receiveTask?.cancel(); socket?.cancel(with: .goingAway, reason: nil)
        socketGeneration = UUID()
        let generation = socketGeneration
        richChat.configure(sessionID: conversationID, connectionID: generation)
        chatTurns.configure(sessionID:conversationID,connectionID:generation);checkingChatStatus = false;chatTurnStatus = "Verifying chat receipt support…"
        configureContext(sessionID: conversationID, connectionID: generation, required: contextRequired(conversationID) || contextSetupPending, requestID: chatTurns.capabilityID)
        contextStatus = contextState.message
        contextDeadline?.cancel()
        capabilityDeadline?.cancel();statusDeadline?.cancel()
        let capabilityID = chatTurns.capabilityID
        capabilityDeadline = Task { [weak self] in
            try? await Task.sleep(nanoseconds:15_000_000_000)
            guard !Task.isCancelled,let self,self.socketGeneration == generation,self.chatTurns.capabilityID == capabilityID,(!self.chatTurns.ready || self.contextState.requestID != nil) else { return }
            if let capabilityID { self.contextState.expire(capabilityID); self.contextStatus = self.contextState.message; self.updateVoiceReadiness() }
            self.recordChatFailure("Chat receipt negotiation timed out. No prompt was sent; reconnect explicitly.");self.chatTurnStatus = "Verified chat unavailable"
        }
        if injectedChatSender != nil {
            do { if let frame = chatTurns.capabilitiesFrame { try await sendChatFrame(frame) } } catch { recordChatFailure("Chat capability verification failed. No task was sent.") }
            return
        }
        var components = URLComponents(url: runtime.baseURL, resolvingAgainstBaseURL: false)!
        components.scheme = "ws"; components.path = "/v1/session"
        components.queryItems = NativeContextCheckpointWire.sessionQuery(conversationID, required: contextManaged || contextSetupPending)
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
                        if let self {
                            if self.chatTurns.isTracked { self.markTrackedUnknown("Chat disconnected before the request receipt arrived. Check status; it has not been retried.") }
                            else { self.recordChatFailure("Chat disconnected. Reconnect explicitly.");self.finishResponse() }
                            self.chatTurns.configure(sessionID:nil,connectionID:nil);self.checkingChatStatus = false
                            self.configureContext(sessionID: self.conversationID, connectionID: nil, required: self.contextManaged, requestID: nil)
                            self.contextStatus = self.contextState.message
                            await self.persistConversation()
                        }
                    }
                    break
                }
            }
        }
        do { if let frame = chatTurns.capabilitiesFrame { try await sendChatFrame(frame) } }
        catch { recordChatFailure("Chat capability verification failed. No task was sent.") }
    }

    // The injected transport is a fixture seam; production always sends through
    // this exact current socket and never chooses execution identity from JSON.
    func reconnectVerifiedChat() async { await connectChat() }
    func refreshContextReadiness() async {
        guard ready, !shuttingDown, let frame = contextState.refreshFrame(), let id = frame["id"] as? String else { return }
        let connection = socketGeneration
        contextStatus = "Checking saved-context readiness…"
        updateVoiceReadiness()
        contextDeadline?.cancel()
        contextDeadline = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 15_000_000_000)
            guard !Task.isCancelled, let self, self.socketGeneration == connection else { return }
            self.contextState.expire(id); self.contextStatus = self.contextState.message; self.updateVoiceReadiness()
        }
        do { try await sendChatFrame(frame) }
        catch { if connection == socketGeneration { contextState.expire(id); contextStatus = contextState.message; updateVoiceReadiness() } }
    }
    func recoverSavedContext(_ review: NativeContextRecoveryReview) async {
        guard canRecoverSavedContext, review == contextRecoveryReview else { return }
        await submitContextRecovery(review)
    }
    func checkSavedContextRecovery() async {
        guard canCheckSavedContextRecovery, let review = contextRecoveryReview else { return }
        await submitContextRecovery(review)
    }
    private func submitContextRecovery(_ review: NativeContextRecoveryReview) async {
        let connection = review.connectionID
        guard review == contextRecoveryReview, connection == socketGeneration,
              review.selectionID == conversationRevision,
              let frame = contextState.recoveryFrame(review: review.fence, connectionID: connection),
              let id = frame["id"] as? String else {
            contextStatus = "This conversation changed. Check its context before reviewing recovery again."
            return
        }
        do {
            guard let primary = recovery.primarySessionID else { throw NativeContextPreferenceFailure() }
            if frame["method"] as? String == "session.context.recover" {
                try contextPreferences.rememberRecovery(review.fence, primary: primary)
            } else {
                guard try contextPreferences.pendingRecovery(session: review.fence.sessionID, primary: primary) == review.fence else { throw NativeContextPreferenceFailure() }
            }
        } catch {
            contextState.expireRecovery(id)
            contextStatus = "Recovery information could not be saved or verified. No request was sent and no stored information was reset."
            updateVoiceReadiness(); return
        }
        contextStatus = contextState.recoveryMessage ?? contextState.message
        updateVoiceReadiness(); contextDeadline?.cancel()
        contextDeadline = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 20_000_000_000)
            guard !Task.isCancelled, let self, self.socketGeneration == connection else { return }
            self.contextState.expireRecovery(id)
            self.contextStatus = self.contextState.recoveryMessage ?? self.contextState.message
            self.updateVoiceReadiness()
        }
        do { try await sendChatFrame(frame) }
        catch {
            guard socketGeneration == connection, conversationRevision == review.selectionID,
                  conversationID == review.fence.sessionID,
                  contextState.recoveryRequestID == id else { return }
            contextDeadline?.cancel(); contextState.expireRecovery(id)
            contextStatus = contextState.recoveryMessage ?? contextState.message
            updateVoiceReadiness()
        }
    }
    private func sendChatFrame(_ frame:[String:Any]) async throws {
        if let injectedChatSender { try await injectedChatSender(frame);return }
        guard let socket,socket.state == .running else { throw NativeFailure("Chat is disconnected.") }
        let data = try JSONSerialization.data(withJSONObject:frame)
        try await socket.send(.string(String(decoding:data,as:UTF8.self)))
    }
    private func recoverSavedChatRequest() {
        unresolvedChatRequest = nil;chatRecoveryBlocked = false;checkingChatStatus = false
        for message in messages where message.role == "user" {
            guard let marker = message.metadata["chat_turn"] as? [String:Any] else { continue }
            if marker["state"] as? String == "not_submitted" { continue }
            if marker["state"] as? String == "terminal",NativeChatTurnWire.boolean(marker["durable"]) == true,let outcome = marker["processing_outcome"] as? String,NativeChatTurnWire.outcomes.contains(outcome) { continue }
            chatRecoveryBlocked = true
            unresolvedChatRequest = NativeTurnReference.restored(marker,sessionID:conversationID)
            chatTurnStatus = "Earlier request outcome needs checking"
            return
        }
        chatTurnStatus = "Connecting verified chat…"
    }
    private func updateRequestMarker(_ reference:NativeTurnReference,record:[String:Any]) {
        guard reference.sessionID == conversationID,let index = messages.firstIndex(where:{ $0.role == "user" && ($0.metadata["chat_turn"] as? [String:Any])?["request_id"] as? String == reference.requestID }) else { return }
        messages[index].metadata["chat_turn"] = record
    }
    private func markTrackedUnknown(_ reason:String) {
        guard let reference = chatTurns.active ?? unresolvedChatRequest else { return }
        var marker = reference.record;marker["state"] = "outcome_unknown"
        updateRequestMarker(reference,record:marker)
        if let id = streamMessageID,let index = messages.firstIndex(where:{$0.id == id}) { messages[index].metadata["responseIncomplete"] = true;messages[index].metadata["deliveryError"] = reason }
        unresolvedChatRequest = reference;chatRecoveryBlocked = true;chatTurnStatus = "Outcome needs checking"
        recordChatFailure(reason);finishResponse()
    }
    func checkChatDeadline(_ reference:NativeTurnReference,connectionID:UUID) async {
        guard connectionID == socketGeneration,chatTurns.active?.requestID == reference.requestID,reference.sessionID == conversationID,isSending else { return }
        markTrackedUnknown("The whole-turn receipt has not arrived within four minutes. Earlier actions may have run. Check status or request Stop; no task was retried.")
        await persistConversation()
    }
    func checkChatStatus() async {
        guard ready,!shuttingDown,!checkingChatStatus,let reference = unresolvedChatRequest else { return }
        if !chatTurns.ready { await connectChat();return }
        guard let frame = chatTurns.statusFrame(reference) else { return }
        let connection = socketGeneration
        checkingChatStatus = true
        statusDeadline?.cancel()
        let statusID = frame["id"] as! String
        statusDeadline = Task { [weak self] in
            try? await Task.sleep(nanoseconds:20_000_000_000)
            guard !Task.isCancelled,let self,self.socketGeneration == connection,self.chatTurns.expireStatus(statusID) else { return }
            self.checkingChatStatus = false;self.recordChatFailure("Status read timed out. Earlier effects remain unknown; no task was retried.")
        }
        do { try await sendChatFrame(frame) }
        catch { if connection == socketGeneration { chatTurns.expireStatus(statusID);statusDeadline?.cancel();checkingChatStatus = false;recordChatFailure("Status could not be read. The earlier task has not been retried.") } }
    }
    private func applyChatTerminal(_ terminal:NativeTurnTerminal) async {
        guard terminal.reference.sessionID == conversationID else { return }
        let connection = socketGeneration
        let observedError = chatError
        let failed = ["failed","cancelled","outcome_unknown","unavailable","budget_exceeded"].contains(terminal.outcome)
        var record = terminal.record;if failed,let observedError { record["reported_error"] = observedError }
        updateRequestMarker(terminal.reference,record:record)
        if let index = messages.firstIndex(where:{ $0.role == "user" && ($0.metadata["chat_turn"] as? [String:Any])?["request_id"] as? String == terminal.reference.requestID }) {
            if failed { messages[index].metadata["deliveryError"] = observedError ?? terminal.summary }
            else { messages[index].metadata.removeValue(forKey:"deliveryError") }
        }
        archiveRichTurn()
        if !terminal.text.isEmpty {
            if let id = streamMessageID,let index = messages.firstIndex(where:{$0.id == id}) { messages[index].text = terminal.text;messages[index].metadata["chat_turn"] = terminal.record }
            else if let index = messages.lastIndex(where:{ $0.role == "assistant" && ($0.metadata["chat_turn"] as? [String:Any])?["request_id"] as? String == terminal.reference.requestID && $0.text == terminal.text }) { streamMessageID = messages[index].id;messages[index].metadata["chat_turn"] = terminal.record }
            else { let id = UUID().uuidString;streamMessageID = id;messages.append(NativeMessage(id:id,role:"assistant",text:terminal.text,metadata:["chat_turn":terminal.record])) }
        }
        archiveRichTurn();finishResponse();statusDeadline?.cancel();checkingChatStatus = false;error = failed ? observedError : nil;chatError = failed ? observedError : nil
        recoverSavedChatRequest()
        if !chatRecoveryBlocked { chatTurnStatus = terminal.summary }
        if failed {
            for index in messages.indices where messages[index].role == "assistant" && (messages[index].metadata["chat_turn"] as? [String:Any])?["request_id"] as? String == terminal.reference.requestID {
                messages[index].metadata["processing_outcome"] = terminal.outcome
                messages[index].metadata["action_outcome"] = terminal.actionOutcome
                if terminal.outcome != "cancelled" || terminal.actionOutcome == "unknown" { messages[index].metadata["responseIncomplete"] = true;messages[index].metadata["deliveryError"] = observedError ?? terminal.summary }
            }
        }
        // Fence the next task before any display persistence suspension.
        if contextManaged { await refreshContextReadiness() }
        guard connection == socketGeneration else { return }
        await persistConversation()
    }

    func consume(_ frame: [String: Any]) async {
        guard preferencesPermitEffects() else { return }
        guard ready, !shuttingDown else { return }
        let owner = runtimeRevision, connection = socketGeneration
        let type = frame["type"] as? String ?? ""
        let payload = frame["payload"] as? [String: Any] ?? [:]
        let globalBudget = type == "state_push" && frame["event"] as? String == "cost_cap_hit"
        if let sid = frame["session_id"] as? String, sid != conversationID, !globalBudget { return }
        if let sid = payload["session_id"] as? String, sid != conversationID, !globalBudget { return }
        if contextState.consumeRecovery(frame, connectionID: connection) {
            contextDeadline?.cancel(); contextStatus = contextState.recoveryMessage ?? contextState.message
            updateVoiceReadiness()
            if !contextState.recoveryUnknown { await refreshContextReadiness() }
            return
        }
        restorePendingContextRecovery()
        let contextChanged = contextState.consume(frame, connectionID: connection)
        if contextChanged {
            contextDeadline?.cancel(); contextStatus = contextState.recoveryMessage ?? contextState.message
            if contextState.managed, let primary = recovery.primarySessionID {
                do {
                    try contextPreferences.remember(conversationID, primary: primary)
                    if let completed = contextState.recoveryReconciledFence {
                        try contextPreferences.clearRecovery(completed, primary: primary)
                        contextState.finishRecoveryPersistence(completed)
                    }
                }
                catch { configureContext(sessionID: conversationID, connectionID: connection, required: true, requestID: nil); contextStatus = error.localizedDescription }
            }
            updateVoiceReadiness()
        }
        let turnEvent = chatTurns.consume(frame,connectionID:connection)
        switch turnEvent {
        case .ready:
            capabilityDeadline?.cancel()
            chatTurnStatus = chatRecoveryBlocked ? "Earlier request outcome needs checking" : "Verified chat ready"
            updateVoiceReadiness()
            if contextSetupPending { await finishContextCreation(connectionID: connection) }
            if unresolvedChatRequest != nil { await checkChatStatus() };return
        case .accepted:
            if let reference = chatTurns.active {
                var marker = reference.record;marker["state"] = "accepted";updateRequestMarker(reference,record:marker)
                chatTurnStatus = chatTurns.stopRequested ? "Requesting Stop…" : "Request accepted; processing…"
                if let abort = chatTurns.pendingAbortFrame() { do { try await sendChatFrame(abort) } catch { markTrackedUnknown("Stop could not be sent. Check status; cancellation is not confirmed.") } }
                await persistConversation()
            };return
        case .stopAcknowledged:chatTurnStatus = "Stop requested; waiting for the terminal receipt";return
        case .terminal(let terminal):await applyChatTerminal(terminal);return
        case .unavailable(let reason):
            if !chatTurns.statusPending { statusDeadline?.cancel() }
            checkingChatStatus = false
            if chatTurns.isTracked || unresolvedChatRequest != nil { markTrackedUnknown(reason);await persistConversation() }
            else { recordChatFailure(reason);chatTurnStatus = "Verified chat unavailable" };return
        case .ignored: if contextChanged { return }; break
        }
        if ["chat_turn_accepted","chat_turn_terminal"].contains(type) { return }
        let trackedProgress = chatTurns.isTracked || chatRecoveryBlocked || payload["chat_turn"] != nil
        if trackedProgress && !globalBudget && !chatTurns.matchesProgress(frame,connectionID:connection) { return }
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
                if !trackedProgress { finishResponse() };await persistConversation()
            }
            return
        }
        if type == "todo_update" {
            if payload["session_id"] as? String == conversationID, let rows = payload["todos"] as? [[String: Any]] { observedTodos = rows }
        } else if type == "stream_delta" {
            guard payload["kind"] as? String != "reasoning" else { return }
            let delta = payload["delta"] as? String ?? ""
            if !delta.isEmpty {
                if streamMessageID == nil { let id = UUID().uuidString; streamMessageID = id; messages.append(NativeMessage(id: id, role: "assistant", text: "",metadata:chatTurns.active.map { ["chat_turn":$0.record] } ?? [:])) }
                if let index = messages.firstIndex(where: { $0.id == streamMessageID }) { messages[index].text += delta }
            }
            if payload["is_final"] as? Bool == true {
                if streamMessageID == nil && !trackedProgress { recordChatFailure("The model returned an empty reply. Choose a different model or retry.") }
                archiveRichTurn()
                archiveResponsePayload(payload)
                if trackedProgress { streamMessageID = nil } else { finishResponse() };await persistConversation()
            }
        } else if type == "text_response" || type == "chat_response" {
            let text = payload["text"] as? String ?? ""
            if !text.isEmpty {
                if let index = messages.firstIndex(where: { $0.id == streamMessageID }) { messages[index].text = text }
                else { let id = UUID().uuidString; streamMessageID = id; messages.append(NativeMessage(id: id, role: "assistant", text: text,metadata:chatTurns.active.map { ["chat_turn":$0.record] } ?? [:])) }
            }
            archiveResponsePayload(payload)
            archiveRichTurn()
            if text.isEmpty && streamMessageID == nil && !trackedProgress { recordChatFailure("The model returned an empty reply. Choose a different model or retry.") }
            if trackedProgress { streamMessageID = nil } else { finishResponse() };await persistConversation()
        } else if type == "error" {
            recordChatFailure(payload["message"] as? String ?? payload["text"] as? String ?? "The agent could not generate a reply. Try a different model.")
            if !trackedProgress { discardPartialResponse();finishResponse() }
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
        guard preferencesPermitEffects() else { throw NativeFailure("Local preferences are unavailable; startup and profile changes are paused.") }
        guard ready, !effectsPaused, !switchingConversation, response.sessionID == conversationID,
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
        guard preferencesPermitEffects() else { return }
        guard ready, !effectsPaused, !uploadingAttachments, !isSending, !switchingConversation else { return }
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
        guard preferencesPermitEffects() else { return false }
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard ready && !shuttingDown && !isSending && !switchingConversation && !uploadingAttachments && !text.isEmpty else { return false }
        if !pendingAttachments.isEmpty && authorizedAttachmentIDs != pendingAttachments.map(\.id) {
            attachmentError = "Review the current attachments before sending their contents to the configured model and fallbacks."
            return false
        }
        guard !chatRecoveryBlocked,unresolvedChatRequest == nil else { recordChatFailure("Check the earlier request's status or choose a new conversation. It has not been retried.");return false }
        if !chatTurns.ready { await connectChat() }
        guard chatCanSend,let reference = chatTurns.beginRequest() else { recordChatFailure("Verified chat capability negotiation is not ready. No task was sent.");return false }
        let connection = socketGeneration,revision = conversationRevision
        archiveRichTurn(); richChat.clearTurnPresentation()
        error = nil; chatError = nil; isSending = true; streamMessageID = nil
        let userID = UUID().uuidString
        pendingUserMessageID = userID
        let attachments = pendingAttachments
        var record: [String: Any] = ["id": userID, "role": "user", "text": text, "content": text]
        if !attachments.isEmpty { record["attachments"] = attachments.map(\.record) }
        var marker = reference.record;marker["state"] = "submitted";record["chat_turn"] = marker
        messages.append(NativeMessage(id: userID, role: "user", text: text, metadata: record))
        guard await persistConversation(),connection == socketGeneration,revision == conversationRevision,chatTurns.active?.requestID == reference.requestID,ready,!shuttingDown else {
            if connection == socketGeneration,revision == conversationRevision {
                marker["state"] = "not_submitted";updateRequestMarker(reference,record:marker);chatTurns.discardUnsubmitted(reference);finishResponse()
                recordChatFailure("The request reference could not be saved before submission. No task was sent; your draft and attachments are retained.")
            };return false
        }
        responseDeadline?.cancel()
        responseDeadline = Task { [weak self] in
            try? await Task.sleep(nanoseconds:240_000_000_000)
            guard !Task.isCancelled else { return };await self?.checkChatDeadline(reference,connectionID:connection)
        }
        do {
            var payload: [String: Any] = ["text": text, "turn_contract_version":1,"context": attachments.isEmpty ? [:] : ["attachment_content_authorized": true]]
            if !attachments.isEmpty { payload["attachments"] = attachments.map(\.record) }
            try await sendChatFrame(["type":"text_command","msg_id":reference.requestID,"hop":"client","session_id":reference.sessionID,"payload":payload])
            if connection == socketGeneration,revision == conversationRevision,reference.sessionID == conversationID {
                let sentIDs = Set(attachments.map(\.id));pendingAttachments.removeAll { sentIDs.contains($0.id) }
            }
            return true
        } catch {
            if connection == socketGeneration,revision == conversationRevision,chatTurns.active?.requestID == reference.requestID {
                markTrackedUnknown("Submission was not confirmed. The task may have started. Check status; its user message and partial output are retained.");await persistConversation()
            }
            return false
        }
    }

    func stopChat() async {
        guard chatTurns.isTracked else { return }
        chatTurnStatus = "Requesting Stop…"
        guard let frame = chatTurns.requestStop() else { return }
        let connection = socketGeneration
        do { try await sendChatFrame(frame) }
        catch { if connection == socketGeneration { markTrackedUnknown("Stop could not be sent. Cancellation is not confirmed; check status.");await persistConversation() } }
    }
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
                pendingContextCreationSID = nil; contextSetupPending = false
                rememberCurrentSelection()
                recoveryStatus = "New isolated conversation. This exact session does not inherit shared-primary history."
                messages = []; pendingAttachments = []; attachmentError = nil; chatError = nil; recoverSavedChatRequest(); await connectChat()
            } catch { self.error = error.localizedDescription }
        }
    }
    func newSavedContextConversation() {
        guard preferencesPermitEffects(), canCreateSavedContextChat, let primary = recovery.primarySessionID else { return }
        switchingConversation = true; conversationRevision = UUID()
        let revision = conversationRevision
        Task {
            defer { if revision == conversationRevision { switchingConversation = false } }
            _ = await conversationSaveTask?.value
            guard ready, !shuttingDown, revision == conversationRevision else { return }
            do {
                let id = contextPreferences.pending(primary: primary) ?? "thread-" + UUID().uuidString.lowercased()
                guard NativeContextCheckpointWire.validID(id) else { throw NativeContextPreferenceFailure() }
                try contextPreferences.remember(id, primary: primary)
                try contextPreferences.setPending(id, primary: primary)
                try recovery.rememberSelection(id)
                conversationID = id; messages = []; pendingAttachments = []; attachmentError = nil; chatError = nil
                pendingContextCreationSID = id; contextSetupPending = true
                recoveryStatus = "Preparing a new text chat with saved context. Existing chat messages are not being imported."
                recoverSavedChatRequest(); await connectChat()
            } catch { self.error = error.localizedDescription }
        }
    }
    private func finishContextCreation(connectionID: UUID) async {
        guard let id = pendingContextCreationSID, contextSetupPending, contextState.capability?.ready == true,
              contextState.managed, socketGeneration == connectionID, id == conversationID,
              let primary = recovery.primarySessionID else { return }
        let revision = conversationRevision
        do {
            // The opt-in create-if-missing route is atomic; the legacy route is an upsert.
            // A winning nonempty UI record is never overwritten or imported.
            guard let receipt = try await request("/api/conversations/new", body: ["id": id, "title": "New chat with saved context", "create_if_missing": true]) as? [String: Any],
                  NativeContextCheckpointWire.boolean(receipt["ok"]) == true, receipt["id"] as? String == id,
                  NativeContextCheckpointWire.boolean(receipt["create_if_missing"]) == true,
                  NativeContextCheckpointWire.boolean(receipt["created"]) != nil,
                  let winning = receipt["conversation"] as? [String: Any], winning["id"] as? String == id,
                  let rows = winning["messages"] as? [Any], rows.isEmpty else {
                throw NativeFailure("New saved-context chat creation collided or was not confirmed. Existing messages were not replaced.")
            }
            guard ready, !shuttingDown, conversationID == id, socketGeneration == connectionID, conversationRevision == revision else { return }
            guard let stored = try await readExactConversation(id), let rows = stored["messages"] as? [Any], rows.isEmpty else {
                throw NativeFailure("The empty saved-context chat could not be verified. No existing history was imported.")
            }
            guard ready, !shuttingDown, conversationID == id, socketGeneration == connectionID, conversationRevision == revision else { return }
            try contextPreferences.setPending(nil, primary: primary)
            pendingContextCreationSID = nil; contextSetupPending = false
            rememberCurrentSelection()
            recoveryStatus = "This new text chat uses verified server-owned saved context. Saved UI messages are not its authority."
            updateVoiceReadiness()
        } catch { if conversationID == id, socketGeneration == connectionID { self.error = error.localizedDescription; contextStatus = "Saved-context setup needs checking. Reconnect this exact new chat; no task was sent." } }
    }

    func openConversation(_ id: String) async {
        guard ready, !shuttingDown, !isSending, !switchingConversation, !uploadingAttachments, !deletedConversationIDs.contains(id) else {
            error = "Wait for attachment uploads or stop the current reply before switching conversations."; return
        }
        switchingConversation = true; conversationRevision = UUID()
        let revision = conversationRevision
        defer { if conversationRevision == revision { switchingConversation = false } }
        _ = await conversationSaveTask?.value
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
        guard !contextManaged, !contextRequired(id), ready, !chatMutationBusy, !id.isEmpty, id.count <= 256,
              !deletedConversationIDs.contains(id), history.count <= 500,
              JSONSerialization.isValidJSONObject(history),
              history.allSatisfy({ ($0["role"] as? String)?.isEmpty == false }) else {
            throw NativeFailure("Stop active chat or voice and finish uploads before applying a valid saved point.")
        }
        applyingSnapshotHistory = true
        switchingConversation = true; conversationRevision = UUID()
        let revision = conversationRevision
        defer { applyingSnapshotHistory = false; if conversationRevision == revision { switchingConversation = false } }
        _ = await conversationSaveTask?.value
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
    @discardableResult private func persistConversation(shutdownSave: Bool = false) async -> Bool {
        let id = conversationID
        guard !id.isEmpty, !deletedConversationIDs.contains(id) else { return false }
        guard !contextSetupPending, contextState.canSaveDisplay else { return false }
        let rows = messages.map(\.savedRecord), owner = runtimeRevision, previous = conversationSaveTask
        let task = Task { [weak self] in
            _ = await previous?.value
            guard let self, self.ready, owner == self.runtimeRevision, !self.deletedConversationIDs.contains(id) else { return false }
            do {
                guard let receipt = try await self.request("/api/conversations/save", body: ["id": id, "messages": rows], shutdownSave: shutdownSave) as? [String: Any], receipt["id"] as? String == id,let count = receipt["message_count"] as? NSNumber,CFGetTypeID(count) != CFBooleanGetTypeID(),["c","s","i","l","q","C","S","I","L","Q"].contains(String(cString:count.objCType)),count.intValue == rows.count else { throw NativeFailure("The saved conversation acknowledgement did not match its ID and exact message count.") }
                return true
            } catch { if owner == self.runtimeRevision, id == self.conversationID { self.error = "Conversation could not be saved: \(error.localizedDescription)" };return false }
        }
        conversationSaveTask = task
        return await task.value
    }

    func saveSettings() async {
        guard preferencesPermitEffects() else { return }
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
        guard preferencesPermitEffects() else { return }
        await saveSettings()
        if error == nil { onboarded = true; prefs.set(true, forKey: "onboarded"); showProviderSetup = true }
    }
    func completeProfileOnboarding() {
        guard preferencesPermitEffects() else { return }
        saveProfile(); onboarded = true; prefs.set(true, forKey: "onboarded"); showProviderSetup = true
    }
    func openProviderSetup() { showProviderSetup = true }
    func dismissProviderSetup() { showProviderSetup = false }
    // Called only after the provider feature has verified its own HTTP receipt.
    // Clearing a local sheet must never mark backend setup complete.
    func completeProviderSetup() { showProviderSetup = false }
    func saveProfile() {
        guard preferencesPermitEffects(), !effectsPaused else { return }
        prefs.set(displayName, forKey: "displayName"); prefs.set(avatarChoice, forKey: "avatarChoice"); prefs.set(importedAvatarPath, forKey: "importedAvatarPath")
    }
    func importAvatar(_ url: URL) {
        guard preferencesPermitEffects(), !effectsPaused else { return }
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
    @discardableResult func flushConversationForShutdown() async -> Bool {
        shuttingDown = true
        receiveTask?.cancel(); socket?.cancel(with: .goingAway, reason: nil)
        archiveRichTurn()
        if let id = streamMessageID, let index = messages.firstIndex(where: { $0.id == id }) {
            messages[index].metadata["responseIncomplete"] = true
            messages[index].metadata["deliveryError"] = "The app closed before this reply completed."
        }
        if chatTurns.isTracked { markTrackedUnknown("The app closed before the whole-turn receipt. Check the stored request status after reopening; it has not been retried.") } else { finishResponse() }
        return await persistConversation(shutdownSave: true)
    }
    func shutdown() async {
        shuttingDown = true
        if let owner = runtime.ownership {
            do { try NativeLocalActionGate.shared.pause(origin: owner.baseURL) }
            catch { profileArchiveError = "Local action admission could not be closed; the owned runtime will be stopped." }
        }
        profileArchiveTask?.cancel()
        await profileArchiveTask?.value
        if let workspaceCenter { for token in workspaceObservers { workspaceCenter.removeObserver(token) } }
        workspaceObservers = []; workspaceCenter = nil
        await flushConversationForShutdown()
        if voice.state != "off" { await voice.stop() }
        voice.configureConnection(sessionID: nil, connected: false)
        richChat.configure(sessionID: nil, connectionID: nil)
        responseDeadline?.cancel(); codingPoll?.cancel(); receiveTask?.cancel(); socket?.cancel(with: .goingAway, reason: nil)
        contextDeadline?.cancel()
        await runtime.stop(); runtimeRevision = UUID(); recovery.configure(baseURL: nil, connectionID: nil); session.invalidateAndCancel(); ready = false; serviceReachable = false
        capabilityDeadline?.cancel();statusDeadline?.cancel()
    }
}

private func pretty(_ value: Any) -> String { guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]) else { return String(describing: value) }; return String(decoding: data, as: UTF8.self) }
