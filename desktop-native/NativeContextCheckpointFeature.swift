import Foundation
import CoreFoundation

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
    var ready: Bool { state == "ready" && checkpoint != nil }
    var permitsSubmission: Bool { ready || (!managed && state == "legacy") }
    var message: String {
        if ready { return "Saved context is ready for this text chat." }
        if !managed && state == "legacy" { return "Standard chat. Saved messages are separate from the agent's runtime context." }
        switch state {
        case "in_progress": return "This chat's saved context needs checking. No task will be retried. Inspect the earlier request or start a new chat."
        case "legacy_unavailable": return "Existing messages cannot be converted into saved runtime context. Start a new chat with saved context."
        case "deleted": return "This saved context was deleted. Start a new chat; it will not be recreated here."
        case "quota": return "Saved-context storage is full. This chat cannot start a task."
        case "corrupt", "conflict", "unsupported": return "This chat's saved context could not be verified. Tasks are paused; status and saved messages remain available."
        default: return "Saved context is unavailable. No task was sent or retried. Check the connection or use a separate standard chat."
        }
    }
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
        guard supported, managed || (!ready && state != "legacy" && state != "ready"),
              let unsupported = payload["managed_unsupported_paths"] as? [String],
              unsupported.count <= 32, Set(unsupported).count == unsupported.count,
              unsupportedPaths.isSubset(of: Set(unsupported)) else { return nil }
        if !ready {
            guard state != "ready", state != "legacy", payload["context_checkpoint"] == nil else { return nil }
            return NativeContextCapability(managed: managed, supported: true, state: state, checkpoint: nil)
        }
        guard state == "ready", let record = payload["context_checkpoint"] as? [String: Any],
              integer(record["contract_version"]) == 1, record["session_id"] as? String == sessionID,
              boolean(record["durable"]) == true, let initialized = boolean(record["initialized"]),
              let generation = record["generation"] as? String, uuid(generation),
              let revision = integer(record["revision"], minimum: 1, maximum: Int64.max - 1),
              let raw = record["omissions"] as? [String: Any], Set(raw.keys) == omissionKeys else { return nil }
        var omissions: [String: Int64] = [:]
        for key in omissionKeys { guard let value = integer(raw[key]) else { return nil }; omissions[key] = value }
        return NativeContextCapability(managed: true, supported: true, state: state,
            checkpoint: NativeContextCheckpoint(sessionID: sessionID, generation: generation,
                revision: revision, initialized: initialized, omissions: omissions))
    }
}

struct NativeContextCheckpointState {
    private(set) var sessionID: String?
    private(set) var connectionID: UUID?
    private(set) var requestID: String?
    private(set) var capability: NativeContextCapability?
    private(set) var required = false
    private(set) var verifiedInitialReady = false
    private var lastCheckpoint: NativeContextCheckpoint?
    var managed: Bool { required || capability?.managed == true }
    var permitsSubmission: Bool { requestID == nil && capability?.permitsSubmission == true }
    var canSaveDisplay: Bool { !managed || verifiedInitialReady }
    var supportsCreation: Bool { capability?.supported == true }
    var message: String { capability?.message ?? (managed ? "Verifying this chat's saved context…" : "Verifying chat capabilities…") }
    mutating func configure(sessionID: String?, connectionID: UUID?, required: Bool, requestID: String?) {
        let sameSession = self.sessionID == sessionID
        self.sessionID = sessionID; self.connectionID = connectionID; self.required = required || (sameSession && self.required)
        self.requestID = requestID; capability = nil
        if !sameSession { lastCheckpoint = nil; verifiedInitialReady = false }
    }
    mutating func refreshFrame() -> [String: Any]? {
        guard sessionID != nil, connectionID != nil, requestID == nil else { return nil }
        let id = UUID().uuidString.lowercased(); requestID = id
        return ["type": "req", "id": id, "method": "chat.capabilities", "params": [:]]
    }
    mutating func expire(_ id: String) {
        guard requestID == id else { return }; requestID = nil
        capability = NativeContextCapability(managed: managed, supported: false, state: "unavailable", checkpoint: nil)
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
    private let key = "native.savedContextModes.v1"
    private let pendingKey = "native.pendingSavedContextCreation.v1"
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
