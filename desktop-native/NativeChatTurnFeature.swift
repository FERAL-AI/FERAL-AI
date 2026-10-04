import Foundation
import CoreFoundation

struct NativeTurnReference {
    let sessionID: String
    let requestID: String
    var turnID: String?
    var record: [String: Any] {
        var value: [String: Any] = ["contract_version": 1, "session_id": sessionID, "request_id": requestID]
        if let turnID { value["turn_id"] = turnID }
        return value
    }
    static func restored(_ value: [String: Any], sessionID: String) -> Self? {
        guard value["session_id"] as? String == sessionID,
              let request = value["request_id"] as? String, NativeChatTurnWire.uuid(request),
              NativeChatTurnWire.version(value["contract_version"]) else { return nil }
        let turn = value["turn_id"] as? String
        if let turn, !NativeChatTurnWire.uuid(turn) { return nil }
        return Self(sessionID: sessionID, requestID: request, turnID: turn)
    }
}

struct NativeTurnTerminal {
    let reference: NativeTurnReference
    let outcome: String
    let text: String
    let actionOutcome: String
    let approvals: [String]
    var replayed: Bool
    var contextCheckpoint: NativeTurnContextCheckpoint? = nil
    var record: [String: Any] {
        var result = reference.record
        result["state"] = "terminal"; result["processing_outcome"] = outcome
        result["action_outcome"] = actionOutcome; result["approval_request_ids"] = approvals
        result["durable"] = true; result["replayed"] = replayed
        if let contextCheckpoint { result["context_checkpoint"] = contextCheckpoint.record }
        return result
    }
    var summary: String {
        switch outcome {
        case "completed": return actionOutcome == "unknown" ? "Processing finished; actions may still need checking" : "Request processing finished"
        case "awaiting_approval": return "Waiting for your approval"
        case "cancelled": return actionOutcome == "unknown" ? "Stopped; earlier actions may still need checking" : "Stopped"
        case "outcome_unknown": return "Outcome needs checking"
        case "refused": return "Request declined"
        case "budget_exceeded": return "Budget limit reached"
        case "unavailable": return "Agent unavailable"
        default: return "Request failed"
        }
    }
}

/// Exact historical commit receipt; never authority to replay a task or audio.
struct NativeTurnContextCheckpoint: Equatable {
    let sessionID: String
    let generation: String
    let revision: Int64
    let attemptID: String
    var record: [String: Any] { ["contract_version": 1, "session_id": sessionID, "generation": generation, "revision": revision, "attempt_id": attemptID, "durable": true] }
    static func parse(_ value: Any?, sessionID: String) -> Self? {
        guard let row = value as? [String: Any], Set(row.keys) == Set(["contract_version", "session_id", "generation", "revision", "attempt_id", "durable"]),
              NativeChatTurnWire.version(row["contract_version"]), row["session_id"] as? String == sessionID,
              NativeChatTurnWire.boolean(row["durable"]) == true,
              let generation = row["generation"] as? String, NativeChatTurnWire.uuid(generation),
              let revision = NativeChatTurnWire.integer(row["revision"]), revision >= 1, revision < Int64.max,
              let attempt = row["attempt_id"] as? String, NativeChatTurnWire.uuid(attempt) else { return nil }
        return Self(sessionID: sessionID, generation: generation, revision: revision, attemptID: attempt)
    }
}

enum NativeChatTurnEvent {
    case ignored, ready, accepted, stopAcknowledged
    case terminal(NativeTurnTerminal)
    case unavailable(String)
}

enum NativeChatTurnWire {
    static func uuid(_ value: String) -> Bool {
        value.count == 36 && UUID(uuidString: value)?.uuidString.lowercased() == value
    }
    static func boolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }
    static func integer(_ value: Any?) -> Int64? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              ["c", "s", "i", "l", "q", "C", "S", "I", "L", "Q"].contains(String(cString: number.objCType)),
              number.compare(NSNumber(value: Int64.max)) != .orderedDescending else { return nil }
        return number.int64Value
    }
    static func version(_ value: Any?) -> Bool {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return false }
        return ["c", "s", "i", "l", "q", "C", "S", "I", "L", "Q"].contains(String(cString: number.objCType)) && number.intValue == 1
    }
    /// Local capture is already stopped when its captured disable is sent.
    /// The host still verifies the current socket/SID; the backend verifies
    /// the exact retired attempt. This predicate confers no start authority.
    static func managedVoiceStop(_ frame: [String: Any]) -> Bool {
        guard frame["type"] as? String == "voice_config", let payload = frame["payload"] as? [String: Any],
              payload["mode"] as? String == "disabled", version(payload["managed_chained_voice_version"]),
              version(payload["voice_attempt_version"]), let attempt = payload["voice_attempt_id"] as? String,
              uuid(attempt) else { return false }
        return true
    }
    static let outcomes: Set<String> = ["completed", "awaiting_approval", "failed", "cancelled", "outcome_unknown", "unavailable", "refused", "budget_exceeded"]
    static func terminal(_ payload: [String: Any], reference: NativeTurnReference) -> NativeTurnTerminal? {
        guard version(payload["contract_version"]), boolean(payload["durable"]) == true,
              payload["session_id"] as? String == reference.sessionID,
              payload["request_id"] as? String == reference.requestID,
              let turn = payload["turn_id"] as? String, uuid(turn),
              reference.turnID == nil || turn == reference.turnID,
              let outcome = payload["processing_outcome"] as? String, outcomes.contains(outcome),
              let text = payload["final_text"] as? String, text.utf8.count <= 8 * 1024 * 1024,
              let action = payload["action_outcome"] as? String, ["not_asserted", "unknown"].contains(action),
              let approvals = payload["approval_request_ids"] as? [String], approvals.count <= 128,
              approvals.allSatisfy({ !$0.isEmpty && $0.utf8.count <= 1024 }),
              let replayed = boolean(payload["replayed"]) else { return nil }
        let checkpoint = NativeTurnContextCheckpoint.parse(payload["context_checkpoint"], sessionID: reference.sessionID)
        if payload["context_checkpoint"] != nil && (checkpoint == nil || !["completed", "awaiting_approval", "refused"].contains(outcome)) { return nil }
        return NativeTurnTerminal(reference: NativeTurnReference(sessionID: reference.sessionID, requestID: reference.requestID, turnID: turn), outcome: outcome, text: text, actionOutcome: action, approvals: approvals, replayed: replayed, contextCheckpoint: checkpoint)
    }
}

/// Processing receipts certify a turn, never reading, seeing or an external effect.
struct NativeChatTurnState {
    private(set) var sessionID: String?
    private(set) var connectionID: UUID?
    private(set) var capabilityID: String?
    private(set) var ready = false
    private(set) var managedChainedVoiceReady = false
    private(set) var active: NativeTurnReference?
    private(set) var stopRequested = false
    private(set) var stopAcknowledged = false
    private var abortID: String?
    private var statusID: String?
    private var statusReference: NativeTurnReference?
    var isTracked: Bool { active != nil }
    var statusPending: Bool { statusID != nil }

    mutating func configure(sessionID: String?, connectionID: UUID?) {
        self.sessionID = sessionID; self.connectionID = connectionID
        capabilityID = sessionID != nil && connectionID != nil ? UUID().uuidString.lowercased() : nil
        ready = false; managedChainedVoiceReady = false; active = nil; stopRequested = false; stopAcknowledged = false
        abortID = nil; statusID = nil; statusReference = nil
    }
    var capabilitiesFrame: [String: Any]? {
        guard let capabilityID else { return nil }
        return ["type": "req", "id": capabilityID, "method": "chat.capabilities", "params": [:]]
    }
    /// Called only after the host consumes a correlated, validated READY
    /// context capability. IN_PROGRESS availability is not a revocation.
    mutating func refreshManagedVoiceCapabilities(_ payload: [String: Any]) {
        guard ready, payload["session_id"] as? String == sessionID,
              NativeChatTurnWire.boolean(payload["context_managed"]) == true,
              NativeChatTurnWire.boolean(payload["context_ready"]) == true,
              payload["context_state"] as? String == "ready" else { return }
        guard let versions = payload["managed_chained_voice_versions"] as? [Any], versions.count <= 16,
              versions.allSatisfy({ NativeChatTurnWire.integer($0).map { $0 >= 1 && $0 <= 1024 } ?? false }) else {
            managedChainedVoiceReady = false; return
        }
        managedChainedVoiceReady = versions.contains { NativeChatTurnWire.version($0) }
    }
    mutating func beginRequest() -> NativeTurnReference? {
        guard ready, let sessionID, connectionID != nil, active == nil else { return nil }
        let reference = NativeTurnReference(sessionID: sessionID, requestID: UUID().uuidString.lowercased())
        active = reference; stopRequested = false; stopAcknowledged = false; abortID = nil
        return reference
    }
    mutating func discardUnsubmitted(_ reference:NativeTurnReference) {
        guard active?.requestID == reference.requestID,active?.turnID == nil else { return }
        active = nil;abortID = nil;stopRequested = false;stopAcknowledged = false
    }
    mutating func requestStop() -> [String: Any]? {
        guard active != nil else { return nil }
        stopRequested = true
        return pendingAbortFrame()
    }
    mutating func pendingAbortFrame() -> [String: Any]? {
        guard ready, stopRequested, abortID == nil, let reference = active, let turn = reference.turnID else { return nil }
        let id = UUID().uuidString.lowercased(); abortID = id
        return ["type": "req", "id": id, "method": "chat.abort", "params": ["request_id": reference.requestID, "turn_id": turn]]
    }
    mutating func statusFrame(_ reference: NativeTurnReference) -> [String: Any]? {
        guard ready, reference.sessionID == sessionID, statusID == nil else { return nil }
        let id = UUID().uuidString.lowercased(); statusID = id; statusReference = reference
        return ["type": "req", "id": id, "method": "chat.status", "params": ["request_id": reference.requestID]]
    }
    @discardableResult mutating func expireStatus(_ id:String) -> Bool {
        guard statusID == id else { return false };statusID = nil;statusReference = nil;return true
    }
    func matchesProgress(_ frame: [String: Any], connectionID: UUID) -> Bool {
        guard self.connectionID == connectionID, let active, let turn = active.turnID,
              frame["session_id"] as? String == active.sessionID,
              let payload = frame["payload"] as? [String: Any],
              let identity = payload["chat_turn"] as? [String: Any] else { return false }
        return NativeChatTurnWire.version(identity["contract_version"]) && identity["request_id"] as? String == active.requestID && identity["turn_id"] as? String == turn
    }
    mutating func consume(_ frame: [String: Any], connectionID: UUID) -> NativeChatTurnEvent {
        guard self.connectionID == connectionID, let sessionID else { return .ignored }
        if let sid = frame["session_id"] as? String, sid != sessionID { return .ignored }
        let type = frame["type"] as? String ?? ""
        let payload = frame["payload"] as? [String: Any] ?? [:]
        if type == "res", frame["id"] as? String == capabilityID, !ready {
            guard NativeChatTurnWire.boolean(frame["ok"]) == true,
                  payload["session_id"] as? String == sessionID,
                  let versions = payload["turn_contract_versions"] as? [Any], versions.contains(where: { NativeChatTurnWire.version($0) }),
                  NativeChatTurnWire.boolean(payload["durable_receipts"]) == true,
                  NativeChatTurnWire.boolean(payload["whole_turn_terminal"]) == true else { return .unavailable("This agent connection does not support verified chat turns. Update the local runtime before sending.") }
            if let versions = payload["managed_chained_voice_versions"] as? [Any], versions.count <= 16,
               versions.allSatisfy({ NativeChatTurnWire.integer($0).map { $0 >= 1 && $0 <= 1024 } ?? false }) {
                managedChainedVoiceReady = versions.contains { NativeChatTurnWire.version($0) }
            }
            ready = true; return .ready
        }
        if type == "res", let statusID, frame["id"] as? String == statusID {
            let reference = statusReference; self.statusID = nil; statusReference = nil
            guard let reference, NativeChatTurnWire.boolean(frame["ok"]) == true,
                  NativeChatTurnWire.boolean(payload["found"]) == true,
                  let receipt = payload["receipt"] as? [String: Any] else { return .unavailable("The earlier request's outcome is not confirmed. It has not been retried.") }
            guard var terminal = NativeChatTurnWire.terminal(receipt, reference: reference) else { return .unavailable("The earlier request is still running or its outcome needs checking. It has not been retried.") }
            terminal.replayed = true // A status read is historical even if the stored receipt originally was live.
            if active?.requestID == reference.requestID { active = nil; abortID = nil;stopRequested = false;stopAcknowledged = false }
            return .terminal(terminal)
        }
        if type == "res", let abortID, frame["id"] as? String == abortID {
            guard let reference = active, NativeChatTurnWire.boolean(frame["ok"]) == true,
                  payload["turn_id"] as? String == reference.turnID,
                  payload["request_id"] as? String == reference.requestID,
                  payload["status"] as? String == "cancel_requested",
                  NativeChatTurnWire.boolean(payload["cancel_requested"]) == true else { return .unavailable("Stop was not confirmed. Check the request status before trying it again.") }
            stopAcknowledged = true; return .stopAcknowledged
        }
        guard ready, let reference = active, payload["request_id"] as? String == reference.requestID else { return .ignored }
        if type == "error" { return .unavailable("The request receipt could not be confirmed. Check its status before trying it again.") }
        guard ["chat_turn_accepted", "chat_turn_terminal"].contains(type) else { return .ignored }
        guard frame["session_id"] as? String == sessionID,
              payload["session_id"] as? String == sessionID,
              NativeChatTurnWire.version(payload["contract_version"]),
              NativeChatTurnWire.boolean(payload["durable"]) == true,
              NativeChatTurnWire.boolean(payload["replayed"]) != nil,
              let turn = payload["turn_id"] as? String, NativeChatTurnWire.uuid(turn),
              reference.turnID == nil || reference.turnID == turn else { return .unavailable("The request receipt did not match this conversation. Its outcome is unconfirmed.") }
        if type == "chat_turn_accepted" {
            guard payload["status"] as? String == "accepted" else { return .unavailable("The request acceptance was not confirmed.") }
            active?.turnID = turn; return .accepted
        }
        guard reference.turnID != nil, let terminal = NativeChatTurnWire.terminal(payload, reference: reference) else { return .unavailable("The final request receipt was incomplete. Its outcome is unconfirmed.") }
        active = nil; abortID = nil;stopRequested = false;stopAcknowledged = false;return .terminal(terminal)
    }
}
