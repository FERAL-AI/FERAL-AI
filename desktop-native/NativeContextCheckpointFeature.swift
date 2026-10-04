import Foundation
import CoreFoundation
import Darwin

struct NativeContextCheckpoint: Equatable {
    let sessionID: String
    let generation: String
    let revision: Int64
    let initialized: Bool
    let omissions: [String: Int64]
}

struct NativeContextCapability {
    let managed: Bool
    let supported: Bool
    let state: String
    let checkpoint: NativeContextCheckpoint?
    var recovery: NativeContextRecoveryFence? = nil
    var ready: Bool { state == "ready" && checkpoint != nil }
    var permitsSubmission: Bool { ready || (!managed && state == "legacy") }
    var message: String {
        if ready { return "Saved context is ready for this text chat." }
        if !managed && state == "legacy" { return "Standard chat. Saved messages are separate from the agent's runtime context." }
        switch state {
        case "in_progress": return "The earlier task was interrupted. Check its outcome before continuing this conversation."
        case "legacy_unavailable": return "Existing messages cannot be converted into saved runtime context. Start a new chat with saved context."
        case "deleted": return "This saved context was deleted. Start a new chat; it will not be recreated here."
        case "quota": return "Saved-context storage is full. This chat cannot start a task."
        case "corrupt", "conflict", "unsupported": return "This chat's saved context could not be verified. Tasks are paused; status and saved messages remain available."
        default: return "Saved context is unavailable. No task was sent or retried. Check the connection or use a separate standard chat."
        }
    }
}

struct NativeContextRecoveryFence: Equatable {
    let sessionID: String
    let generation: String
    let revision: Int64
    let attemptID: String
    var parameters: [String: Any] {
        ["contract_version": 1, "session_id": sessionID, "generation": generation,
         "revision": revision, "attempt_id": attemptID, "acknowledge_unknown_effects": true]
    }
    static let disclosure = "Continue from the last saved agent context. The interrupted task will not run again. Earlier actions may already have happened; check any purchase, booking or message before requesting it again. Old approvals cannot authorize a new task."
    static func parse(_ value: Any?, sessionID: String) -> Self? {
        guard let row = value as? [String: Any],
              NativeContextCheckpointWire.integer(row["contract_version"]) == 1,
              row["session_id"] as? String == sessionID,
              let generation = row["generation"] as? String, NativeContextCheckpointWire.uuid(generation),
              let revision = NativeContextCheckpointWire.integer(row["revision"], minimum: 1, maximum: Int64.max - 2),
              let attemptID = row["attempt_id"] as? String, NativeContextCheckpointWire.uuid(attemptID),
              row["state"] as? String == "in_progress",
              NativeContextCheckpointWire.boolean(row["durable"]) == true,
              NativeContextCheckpointWire.boolean(row["requires_unknown_effects_acknowledgement"]) == true else { return nil }
        return Self(sessionID: sessionID, generation: generation, revision: revision, attemptID: attemptID)
    }
}

struct NativeContextRecoveryReview: Equatable {
    let fence: NativeContextRecoveryFence
    let connectionID: UUID
    let selectionID: UUID
}

enum NativeContextCheckpointWire {
    static let omissionKeys: Set<String> = ["history_rows", "system_rows", "images", "working_rows"]
    static let states: Set<String> = ["ready", "legacy", "legacy_unavailable", "in_progress", "deleted", "corrupt", "unsupported", "quota", "conflict", "unavailable"]
    static let unsupportedPaths: Set<String> = ["voice", "handoff", "reset", "compact", "snapshot", "branch", "restore", "delete"]
    static let disclosure = "This creates a new text chat whose agent context can be retained locally across restarts. It does not convert existing chats or replay tasks. Saved context is bounded; images and some older context may be omitted. Voice, device handoff, saved-point actions and deletion are not supported for this chat yet. Standard chats keep their current features."
    static func boolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }
    static func integer(_ value: Any?, minimum: Int64 = 0, maximum: Int64 = Int64.max) -> Int64? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              ["c", "s", "i", "l", "q", "C", "S", "I", "L", "Q"].contains(String(cString: number.objCType)),
              number.compare(NSNumber(value: minimum)) != .orderedAscending,
              number.compare(NSNumber(value: maximum)) != .orderedDescending else { return nil }
        return number.int64Value
    }
    static func uuid(_ value: String) -> Bool { value.count == 36 && UUID(uuidString: value)?.uuidString.lowercased() == value }
    static func validID(_ value: String) -> Bool { !value.isEmpty && value.utf8.count <= 256 && value.trimmingCharacters(in: .whitespacesAndNewlines) == value && !value.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) }
    static func sessionQuery(_ sessionID: String, required: Bool) -> [URLQueryItem] {
        [URLQueryItem(name: "session_id", value: sessionID)] + (required ? [URLQueryItem(name: "context_checkpoint_version", value: "1")] : [])
    }
    static func parse(_ payload: [String: Any], sessionID: String, required: Bool) -> NativeContextCapability? {
        guard payload["session_id"] as? String == sessionID else { return nil }
        // Older receipt-capable runtimes remain usable for ordinary chats only.
        if payload["context_managed"] == nil && payload["context_checkpoint_versions"] == nil {
            return required ? nil : NativeContextCapability(managed: false, supported: false, state: "legacy", checkpoint: nil)
        }
        guard let managed = boolean(payload["context_managed"]),
              let advertised = payload["context_checkpoint_versions"] as? [Any],
              advertised.count <= 16, advertised.allSatisfy({ integer($0, minimum: 1, maximum: 1024) != nil }),
              let ready = boolean(payload["context_ready"]), let state = payload["context_state"] as? String, states.contains(state) else { return nil }
        let supported = advertised.contains { integer($0) == 1 }
        if !managed && !required && ["legacy", "unavailable"].contains(state) {
            guard !ready, payload["context_checkpoint"] == nil, ["legacy", "unavailable"].contains(state) else { return nil }
            return NativeContextCapability(managed: false, supported: supported, state: "legacy", checkpoint: nil)
        }
        var requiredUnsupported = unsupportedPaths
        if let voiceVersions = payload["managed_chained_voice_versions"] as? [Any],
           voiceVersions.count <= 16,
           voiceVersions.allSatisfy({ integer($0, minimum: 1, maximum: 1024) != nil }),
           voiceVersions.contains(where: { integer($0) == 1 }) {
            requiredUnsupported.remove("voice")
            requiredUnsupported.insert("realtime_voice")
        }
        guard supported, managed || (!ready && state != "legacy" && state != "ready"),
              let unsupported = payload["managed_unsupported_paths"] as? [String],
              unsupported.count <= 32, Set(unsupported).count == unsupported.count,
              requiredUnsupported.isSubset(of: Set(unsupported)) else { return nil }
        if !ready {
            guard state != "ready", state != "legacy", payload["context_checkpoint"] == nil else { return nil }
            var result = NativeContextCapability(managed: managed, supported: true, state: state, checkpoint: nil)
            if let recovery = payload["context_recovery"] {
                guard managed, state == "in_progress",
                      let versions = payload["context_recovery_versions"] as? [Any], versions.count <= 16,
                      versions.allSatisfy({ integer($0, minimum: 1, maximum: 1024) != nil }),
                      versions.contains(where: { integer($0) == 1 }),
                      let fence = NativeContextRecoveryFence.parse(recovery, sessionID: sessionID) else { return nil }
                result.recovery = fence
            }
            return result
        }
        guard state == "ready", payload["context_recovery"] == nil,
              let record = payload["context_checkpoint"] as? [String: Any],
              let checkpoint = parseCheckpoint(record, sessionID: sessionID) else { return nil }
        return NativeContextCapability(managed: true, supported: true, state: state, checkpoint: checkpoint)
    }
    static func parseCheckpoint(_ record: [String: Any], sessionID: String) -> NativeContextCheckpoint? {
        guard
              integer(record["contract_version"]) == 1, record["session_id"] as? String == sessionID,
              boolean(record["durable"]) == true, let initialized = boolean(record["initialized"]),
              let generation = record["generation"] as? String, uuid(generation),
              let revision = integer(record["revision"], minimum: 1, maximum: Int64.max - 1),
              let raw = record["omissions"] as? [String: Any], Set(raw.keys) == omissionKeys else { return nil }
        var omissions: [String: Int64] = [:]
        for key in omissionKeys { guard let value = integer(raw[key]) else { return nil }; omissions[key] = value }
        return NativeContextCheckpoint(sessionID: sessionID, generation: generation,
            revision: revision, initialized: initialized, omissions: omissions)
    }
}

struct NativeContextCheckpointState {
    private(set) var sessionID: String?
    private(set) var connectionID: UUID?
    private(set) var requestID: String?
    private(set) var capability: NativeContextCapability?
    private(set) var required = false
    private(set) var verifiedInitialReady = false
    private(set) var recoveryRequestID: String?
    private(set) var recoveryUnknown = false
    private(set) var recoveryMessage: String?
    private(set) var recoveryReconciledFence: NativeContextRecoveryFence?
    private var reviewedRecovery: NativeContextRecoveryFence?
    private var recoveredCheckpoint: NativeContextCheckpoint?
    private var recoveryStatusQuery = false
    private var lastCheckpoint: NativeContextCheckpoint?
    var managed: Bool { required || capability?.managed == true }
    var permitsSubmission: Bool { requestID == nil && recoveryRequestID == nil && recoveredCheckpoint == nil && recoveryReconciledFence == nil && !recoveryUnknown && capability?.permitsSubmission == true }
    var trackedRecoveryFence: NativeContextRecoveryFence? { reviewedRecovery }
    var recoveryFence: NativeContextRecoveryFence? { recoveryUnknown ? reviewedRecovery : capability?.recovery }
    var canRecover: Bool { requestID == nil && recoveryRequestID == nil && capability?.recovery != nil && !recoveryUnknown }
    var canCheckRecovery: Bool { requestID == nil && recoveryRequestID == nil && recoveryUnknown && reviewedRecovery != nil }
    var canSaveDisplay: Bool { !managed || verifiedInitialReady }
    var supportsCreation: Bool { capability?.supported == true }
    var message: String { capability?.message ?? (managed ? "Verifying this chat's saved context…" : "Verifying chat capabilities…") }
    mutating func configure(sessionID: String?, connectionID: UUID?, required: Bool, requestID: String?) {
        let sameSession = self.sessionID == sessionID
        self.sessionID = sessionID; self.connectionID = connectionID; self.required = required || (sameSession && self.required)
        self.requestID = requestID; capability = nil
        if recoveryRequestID != nil { recoveryRequestID = nil; recoveryUnknown = true; recoveryMessage = "Recovery lost its connection. Check recovery; the earlier task has not been retried." }
        if !sameSession { lastCheckpoint = nil; verifiedInitialReady = false; reviewedRecovery = nil; recoveredCheckpoint = nil; recoveryUnknown = false; recoveryMessage = nil; recoveryReconciledFence = nil }
    }
    mutating func refreshFrame() -> [String: Any]? {
        guard sessionID != nil, connectionID != nil, requestID == nil, recoveryRequestID == nil else { return nil }
        let id = UUID().uuidString.lowercased(); requestID = id
        return ["type": "req", "id": id, "method": "chat.capabilities", "params": [:]]
    }
    mutating func expire(_ id: String) {
        guard requestID == id else { return }; requestID = nil
        capability = NativeContextCapability(managed: managed, supported: false, state: "unavailable", checkpoint: nil)
    }
    mutating func recoveryFrame(review: NativeContextRecoveryFence, connectionID: UUID) -> [String: Any]? {
        guard self.connectionID == connectionID, sessionID == review.sessionID,
              recoveryRequestID == nil, requestID == nil,
              (canRecover && capability?.recovery == review) || (canCheckRecovery && reviewedRecovery == review) else { return nil }
        let id = UUID().uuidString.lowercased()
        recoveryStatusQuery = canCheckRecovery
        reviewedRecovery = review; recoveryRequestID = id; recoveryUnknown = false
        recoveryMessage = "Continuing from saved context; checking the recovery result…"
        var params = review.parameters
        if recoveryStatusQuery { params.removeValue(forKey: "acknowledge_unknown_effects") }
        return ["type": "req", "id": id, "method": recoveryStatusQuery ? "session.context.recoveryStatus" : "session.context.recover", "params": params]
    }
    mutating func expireRecovery(_ id: String) {
        guard recoveryRequestID == id else { return }
        recoveryRequestID = nil; recoveryUnknown = true
        recoveryMessage = "Recovery outcome needs checking. The earlier task has not been retried."
        capability = NativeContextCapability(managed: true, supported: true, state: "unavailable", checkpoint: nil)
    }
    mutating func restorePendingRecovery(_ fence: NativeContextRecoveryFence) {
        guard sessionID == fence.sessionID, recoveryRequestID == nil else { return }
        reviewedRecovery = fence; recoveryUnknown = true; required = true; recoveryReconciledFence = nil
        recoveryMessage = "An earlier recovery outcome needs checking. The task has not been retried."
    }
    mutating func blockRecoveryPreferences() {
        required = true; recoveryUnknown = true
        recoveryMessage = "Saved recovery information could not be verified. Tasks remain paused; no stored information was reset."
    }
    mutating func finishRecoveryPersistence(_ fence: NativeContextRecoveryFence) {
        guard recoveryReconciledFence == fence else { return }
        recoveryReconciledFence = nil
    }
    @discardableResult mutating func consumeRecovery(_ frame: [String: Any], connectionID: UUID) -> Bool {
        guard self.connectionID == connectionID, let sid = sessionID,
              let id = recoveryRequestID, frame["type"] as? String == "res", frame["id"] as? String == id,
              frame["session_id"] == nil || frame["session_id"] as? String == sid,
              let review = reviewedRecovery else { return false }
        if recoveryStatusQuery, NativeContextCheckpointWire.boolean(frame["ok"]) == true,
           let payload = frame["payload"] as? [String: Any], payload["session_id"] as? String == sid,
           NativeContextCheckpointWire.integer(payload["contract_version"]) == 1,
           NativeContextCheckpointWire.boolean(payload["durable"]) == true,
           NativeContextCheckpointWire.boolean(payload["replayed"]) == false,
           NativeContextCheckpointWire.boolean(payload["recovered"]) == false,
           NativeContextCheckpointWire.boolean(payload["context_ready"]) == false,
           payload["action_outcome"] as? String == "unknown", payload["context_checkpoint"] == nil,
           let status = payload["status"] as? String, ["not_recovered", "superseded"].contains(status) {
            recoveryRequestID = nil; recoveryUnknown = status == "superseded"; capability = nil
            recoveryMessage = status == "not_recovered" ? "Recovery did not happen. Refreshing the interrupted conversation for another review." : "Other work changed this conversation. Its effects need checking before continuation."
            return true
        }
        guard NativeContextCheckpointWire.boolean(frame["ok"]) == true,
              let payload = frame["payload"] as? [String: Any], payload["session_id"] as? String == sid,
              NativeContextCheckpointWire.integer(payload["contract_version"]) == 1,
              payload["status"] as? String == "recovered",
              NativeContextCheckpointWire.boolean(payload["context_ready"]) == true,
              NativeContextCheckpointWire.boolean(payload["durable"]) == true,
              NativeContextCheckpointWire.boolean(payload["replayed"]) == false,
              !recoveryStatusQuery || NativeContextCheckpointWire.boolean(payload["recovered"]) == true,
              payload["action_outcome"] as? String == "unknown",
              let record = payload["context_checkpoint"] as? [String: Any],
              let checkpoint = NativeContextCheckpointWire.parseCheckpoint(record, sessionID: sid),
              checkpoint.generation != review.generation, checkpoint.revision == review.revision + 1,
              let attempt = record["attempt_id"] as? String, NativeContextCheckpointWire.uuid(attempt), attempt != review.attemptID else {
            expireRecovery(id); return true
        }
        recoveryRequestID = nil; recoveredCheckpoint = checkpoint; lastCheckpoint = checkpoint
        capability = nil; recoveryUnknown = false
        recoveryMessage = "Recovery acknowledged. Verifying the saved context before another task…"
        return true
    }
    @discardableResult mutating func consume(_ frame: [String: Any], connectionID: UUID) -> Bool {
        guard self.connectionID == connectionID, let sid = sessionID,
              frame["type"] as? String == "res", let id = requestID, frame["id"] as? String == id else { return false }
        requestID = nil
        // Positive mode is sticky even when the rest of its capability is malformed.
        if NativeContextCheckpointWire.boolean(frame["ok"]) == true,
           frame["session_id"] == nil || frame["session_id"] as? String == sid,
           let payload = frame["payload"] as? [String: Any], payload["session_id"] as? String == sid,
           NativeContextCheckpointWire.boolean(payload["context_managed"]) == true { required = true }
        guard NativeContextCheckpointWire.boolean(frame["ok"]) == true,
              frame["session_id"] == nil || frame["session_id"] as? String == sid,
              let payload = frame["payload"] as? [String: Any],
              let parsed = NativeContextCheckpointWire.parse(payload, sessionID: sid, required: required) else {
            capability = NativeContextCapability(managed: managed, supported: false, state: "unavailable", checkpoint: nil); return true
        }
        if parsed.managed { required = true }
        if recoveredCheckpoint == nil && !recoveryUnknown { recoveryMessage = nil }
        if let expected = recoveredCheckpoint {
            guard let actual = parsed.checkpoint, actual.generation == expected.generation, actual.revision == expected.revision else {
                capability = NativeContextCapability(managed: true, supported: true, state: "conflict", checkpoint: nil)
                recoveryMessage = "Recovery readback changed. Tasks remain paused; check this conversation."
                return true
            }
            recoveryReconciledFence = reviewedRecovery
            recoveredCheckpoint = nil; reviewedRecovery = nil; recoveryUnknown = false
            recoveryMessage = "Conversation ready. The interrupted task was not replayed; its earlier effects remain uncertain."
        }
        if let checkpoint = parsed.checkpoint, let previous = lastCheckpoint,
           checkpoint.generation != previous.generation || checkpoint.revision < previous.revision {
            capability = NativeContextCapability(managed: true, supported: true, state: "conflict", checkpoint: nil); return true
        }
        capability = parsed
        if let checkpoint = parsed.checkpoint { lastCheckpoint = checkpoint; verifiedInitialReady = true }
        return true
    }
}

struct NativeContextCheckpointPreferences {
    let defaults: UserDefaults
    let recoveryJournalURL: URL?
    init(defaults: UserDefaults, recoveryJournalURL: URL? = nil) {
        self.defaults = defaults; self.recoveryJournalURL = recoveryJournalURL
    }
    private let key = "native.savedContextModes.v1"
    private let pendingKey = "native.pendingSavedContextCreation.v1"
    private let recoveryKey = "native.pendingContextRecovery.v1"
    private func recoveryRecords() throws -> [String: [String: [String: Any]]] {
        if let url = recoveryJournalURL {
            return try NativeContextRecoveryJournal.access(url, validate: Self.validatedRecoveryRecords)
        }
        return try Self.validatedRecoveryRecords(defaults.object(forKey: recoveryKey))
    }
    private static func validatedRecoveryRecords(_ stored: Any?) throws -> [String: [String: [String: Any]]] {
        guard let stored else { return [:] }
        guard let all = stored as? [String: [String: [String: Any]]], all.count <= 16 else { throw NativeContextPreferenceFailure() }
        for (primary, records) in all {
            guard NativeContextCheckpointWire.validID(primary), records.count <= 1000 else { throw NativeContextPreferenceFailure() }
            for (session, row) in records {
                guard Self.recoveryFence(row, sessionID: session) != nil else { throw NativeContextPreferenceFailure() }
            }
        }
        return all
    }
    private func updateRecoveryRecords(_ update: @escaping (inout [String: [String: [String: Any]]]) throws -> Void) throws {
        if let url = recoveryJournalURL {
            _ = try NativeContextRecoveryJournal.access(url, validate: Self.validatedRecoveryRecords, update: update)
        } else {
            var all = try recoveryRecords(); try update(&all)
            defaults.set(all, forKey: recoveryKey)
        }
    }
    private static func recoveryFence(_ row: [String: Any], sessionID: String) -> NativeContextRecoveryFence? {
        guard NativeContextCheckpointWire.validID(sessionID),
              Set(row.keys) == ["contract_version", "session_id", "generation", "revision", "attempt_id", "acknowledge_unknown_effects"],
              NativeContextCheckpointWire.boolean(row["acknowledge_unknown_effects"]) == true else { return nil }
        var pending = row
        pending["state"] = "in_progress"; pending["durable"] = true
        pending["requires_unknown_effects_acknowledgement"] = true
        return NativeContextRecoveryFence.parse(pending, sessionID: sessionID)
    }
    func pendingRecovery(session: String, primary: String) throws -> NativeContextRecoveryFence? {
        guard NativeContextCheckpointWire.validID(primary), NativeContextCheckpointWire.validID(session) else { throw NativeContextPreferenceFailure() }
        guard let row = try recoveryRecords()[primary]?[session] else { return nil }
        guard let fence = Self.recoveryFence(row, sessionID: session) else { throw NativeContextPreferenceFailure() }
        return fence
    }
    func rememberRecovery(_ fence: NativeContextRecoveryFence, primary: String) throws {
        guard NativeContextCheckpointWire.validID(primary), Self.recoveryFence(fence.parameters, sessionID: fence.sessionID) == fence else { throw NativeContextPreferenceFailure() }
        try updateRecoveryRecords { all in
            guard all[primary] != nil || all.count < 16 else { throw NativeContextPreferenceFailure() }
            var records = all[primary] ?? [:]
            guard records[fence.sessionID] != nil || records.count < 1000 else { throw NativeContextPreferenceFailure() }
            if let existing = records[fence.sessionID], Self.recoveryFence(existing, sessionID: fence.sessionID) != fence { throw NativeContextPreferenceFailure() }
            records[fence.sessionID] = fence.parameters; all[primary] = records
        }
        guard try pendingRecovery(session: fence.sessionID, primary: primary) == fence else { throw NativeContextPreferenceFailure() }
    }
    func clearRecovery(_ fence: NativeContextRecoveryFence, primary: String) throws {
        guard NativeContextCheckpointWire.validID(primary) else { throw NativeContextPreferenceFailure() }
        try updateRecoveryRecords { all in
            guard let existing = all[primary]?[fence.sessionID], Self.recoveryFence(existing, sessionID: fence.sessionID) == fence else { throw NativeContextPreferenceFailure() }
            all[primary]?[fence.sessionID] = nil
        }
        guard try pendingRecovery(session: fence.sessionID, primary: primary) == nil else { throw NativeContextPreferenceFailure() }
    }
    func required(_ session: String, primary: String?) -> Bool {
        guard let primary, let all = defaults.dictionary(forKey: key) as? [String: [String]], let ids = all[primary] else { return false }
        return ids.contains(session)
    }
    func remember(_ session: String, primary: String) throws {
        guard NativeContextCheckpointWire.validID(session), NativeContextCheckpointWire.validID(primary) else { throw NativeContextPreferenceFailure() }
        let stored = defaults.object(forKey: key)
        guard stored == nil || stored is [String: [String]] else { throw NativeContextPreferenceFailure() }
        var all = stored as? [String: [String]] ?? [:]
        guard all[primary] != nil || all.count < 16 else { throw NativeContextPreferenceFailure() }
        var ids = all[primary] ?? []
        guard ids.count <= 1000, all.count <= 16 else { throw NativeContextPreferenceFailure() }
        if !ids.contains(session) { guard ids.count < 1000 else { throw NativeContextPreferenceFailure() }; ids.append(session) }
        all[primary] = ids; defaults.set(all, forKey: key)
        guard required(session, primary: primary) else { throw NativeContextPreferenceFailure() }
    }
    func pending(primary: String) -> String? { (defaults.dictionary(forKey: pendingKey) as? [String: String])?[primary] }
    func setPending(_ session: String?, primary: String) throws {
        guard NativeContextCheckpointWire.validID(primary), session == nil || NativeContextCheckpointWire.validID(session!) else { throw NativeContextPreferenceFailure() }
        let stored = defaults.object(forKey: pendingKey)
        guard stored == nil || stored is [String: String] else { throw NativeContextPreferenceFailure() }
        var all = stored as? [String: String] ?? [:]
        guard session == nil || all[primary] != nil || all.count < 16 else { throw NativeContextPreferenceFailure() }
        guard all.count <= 16 else { throw NativeContextPreferenceFailure() }
        all[primary] = session; defaults.set(all, forKey: pendingKey)
        guard pending(primary: primary) == session else { throw NativeContextPreferenceFailure() }
    }
}

// Only pending recovery identities live here; no credentials or runtime context.
private enum NativeContextRecoveryJournal {
    typealias Records = [String: [String: [String: Any]]]
    private static let maximumBytes = 8 * 1024 * 1024
    private static func regular(_ fd: Int32) throws -> stat {
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              info.st_uid == getuid(), info.st_nlink == 1,
              info.st_mode & 0o077 == 0, info.st_size >= 0,
              info.st_size <= maximumBytes else { throw NativeContextPreferenceFailure() }
        return info
    }
    static func access(_ url: URL, validate: (Any?) throws -> Records,
                       update: ((inout Records) throws -> Void)? = nil) throws -> Records {
        guard url.isFileURL, url.path.hasPrefix("/") else { throw NativeContextPreferenceFailure() }
        let components = url.path.split(separator: "/")
        guard let filename = components.last, !components.contains("."), !components.contains(".."),
              components.count <= 64, filename.utf8.count <= 240 else { throw NativeContextPreferenceFailure() }
        var parent = Darwin.open("/", O_RDONLY | O_DIRECTORY)
        guard parent >= 0 else { throw NativeContextPreferenceFailure() }
        defer { Darwin.close(parent) }
        for component in components.dropLast() {
            let child = Darwin.openat(parent, String(component), O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
            guard child >= 0 else { throw NativeContextPreferenceFailure() }
            Darwin.close(parent); parent = child
        }
        let name = String(filename)
        let lock = Darwin.openat(parent, name + ".lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_NONBLOCK, 0o600)
        guard lock >= 0 else { throw NativeContextPreferenceFailure() }
        defer { Darwin.close(lock) }
        _ = try regular(lock)
        guard flock(lock, LOCK_EX | LOCK_NB) == 0 else { throw NativeContextPreferenceFailure() }
        defer { _ = flock(lock, LOCK_UN) }
        let fd = Darwin.openat(parent, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        var all: Records
        if fd < 0 {
            guard errno == ENOENT else { throw NativeContextPreferenceFailure() }
            all = try validate(nil)
        } else {
            let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
            defer { try? handle.close() }
            let before = try regular(fd)
            let bytes = try handle.read(upToCount: maximumBytes + 1) ?? Data()
            let after = try regular(fd)
            guard bytes.count == before.st_size, before.st_size == after.st_size,
                  before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec,
                  before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec,
                  before.st_ctimespec.tv_sec == after.st_ctimespec.tv_sec,
                  before.st_ctimespec.tv_nsec == after.st_ctimespec.tv_nsec else { throw NativeContextPreferenceFailure() }
            all = try validate(JSONSerialization.jsonObject(with: bytes))
        }
        guard let update else { return all }
        try update(&all)
        _ = try validate(all)
        let bytes = try JSONSerialization.data(withJSONObject: all, options: [.sortedKeys])
        guard bytes.count <= maximumBytes else { throw NativeContextPreferenceFailure() }
        let temporary = "." + name + "." + UUID().uuidString.lowercased()
        let target = Darwin.openat(parent, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard target >= 0 else { throw NativeContextPreferenceFailure() }
        defer { _ = Darwin.unlinkat(parent, temporary, 0) }
        let output = FileHandle(fileDescriptor: target, closeOnDealloc: true)
        defer { try? output.close() }
        try output.write(contentsOf: bytes)
        guard fsync(target) == 0,
              Darwin.renameat(parent, temporary, parent, name) == 0,
              fsync(parent) == 0 else { throw NativeContextPreferenceFailure() }
        return all
    }
}
struct NativeContextPreferenceFailure: LocalizedError {
    var errorDescription: String? { "The saved-context mode could not be retained. No thread was converted and no task was sent." }
}

// A UI dispatch fence, not a runtime checkpoint or permission grant.
struct NativeSelectedContextPolicy: Equatable {
    let sessionID: String?
    let connectionID: UUID?
    let taskReady: Bool
    let managed: Bool
    static let legacy = Self(sessionID: nil, connectionID: nil, taskReady: true, managed: false)
    var message: String {
        taskReady ? "Global settings and background work retain their own scope; they are not this chat's saved context." : "This chat is not ready to start work. Inspection, global settings and recovery controls remain available."
    }
}
struct NativeContextActionGate {
    private(set) var policy = NativeSelectedContextPolicy.legacy
    private(set) var revision = UUID()
    @discardableResult mutating func update(_ value: NativeSelectedContextPolicy) -> Bool {
        guard policy != value else { return false }; policy = value; revision = UUID(); return true
    }
    func accepts(_ revision: UUID) -> Bool { self.revision == revision }
}
