import Foundation

private struct Failure: Error { let message: String }
private func check(_ value: @autoclosure () -> Bool, _ message: String) throws {
    if !value() { throw Failure(message: message) }
}

@main struct NativeChatTurnTests {
    static func capability(_ state: NativeChatTurnState, versions: [Any] = [1]) -> [String: Any] {
        ["type": "res", "id": state.capabilityID ?? "", "ok": true,
         "payload": ["session_id": "A", "turn_contract_versions": versions, "durable_receipts": true, "whole_turn_terminal": true]]
    }
    static func receipt(_ reference: NativeTurnReference, type: String, turn: String, outcome: String = "completed") -> [String: Any] {
        var payload = reference.record
        payload["turn_id"] = turn; payload["durable"] = true; payload["replayed"] = false
        if type == "chat_turn_accepted" { payload["status"] = "accepted" }
        else {
            payload["processing_outcome"] = outcome; payload["final_text"] = "Exact response"
            payload["action_outcome"] = "not_asserted"; payload["approval_request_ids"] = [String]()
        }
        return ["type": type, "session_id": reference.sessionID, "payload": payload]
    }
    static func main() throws {
        let connection = UUID(), turn = UUID().uuidString.lowercased()
        var state = NativeChatTurnState()
        state.configure(sessionID: "A", connectionID: connection)
        try check(state.beginRequest() == nil, "prompt before capability negotiation")
        _ = state.consume(capability(state, versions: [true, 1.0]), connectionID: connection)
        try check(!state.ready, "boolean or floating version accepted")
        _ = state.consume(capability(state), connectionID: UUID())
        try check(!state.ready, "stale connection enabled send")
        _ = state.consume(capability(state), connectionID: connection)
        try check(state.ready, "valid capability refused")
        let request = state.beginRequest()!
        try check(state.beginRequest() == nil, "overlapping prompt accepted")
        try check(state.requestStop() == nil && state.stopRequested, "early stop fabricated server identity")
        let foreign = NativeTurnReference(sessionID: "A", requestID: UUID().uuidString.lowercased())
        _ = state.consume(receipt(foreign, type: "chat_turn_accepted", turn: turn), connectionID: connection)
        try check(state.active?.turnID == nil, "foreign request acquired abort identity")
        _ = state.consume(receipt(request, type: "chat_turn_accepted", turn: turn), connectionID: connection)
        let abort = state.pendingAbortFrame()!
        let params = abort["params"] as! [String: Any]
        try check(params["request_id"] as? String == request.requestID && params["turn_id"] as? String == turn, "abort not exact accepted request")
        try check(state.pendingAbortFrame() == nil, "abort automatically replayed")
        let ack: [String: Any] = ["type": "res", "id": abort["id"]!, "ok": true, "payload": ["request_id": request.requestID, "turn_id": turn, "status": "cancel_requested", "cancel_requested": true]]
        _ = state.consume(ack, connectionID: connection)
        try check(state.stopAcknowledged && state.active != nil, "abort acknowledgement claimed terminal cancellation")
        let unscoped: [String: Any] = ["type": "stream_delta", "session_id": "A", "payload": ["delta": "other turn", "is_final": true]]
        try check(!state.matchesProgress(unscoped, connectionID: connection), "unscoped provider final accepted")
        let progress: [String: Any] = ["type": "stream_delta", "session_id": "A", "payload": ["delta": "partial", "chat_turn": ["contract_version": 1, "request_id": request.requestID, "turn_id": turn]]]
        try check(state.matchesProgress(progress, connectionID: connection) && !state.matchesProgress(progress, connectionID: UUID()), "progress connection fence wrong")
        let cancelled = receipt(request, type: "chat_turn_terminal", turn: turn, outcome: "cancelled")
        guard case .terminal(let result) = state.consume(cancelled, connectionID: connection) else { throw Failure(message: "correlated cancellation refused") }
        try check(result.outcome == "cancelled" && state.active == nil && result.actionOutcome == "not_asserted", "cancel fabricated effects or retained active request")
        guard case .ignored = state.consume(ack, connectionID: connection) else { throw Failure(message: "late abort changed finished response") }
        print("PASS negotiation, exact ownership, early Stop, no duplicate abort and processing-only terminal")

        let next = state.beginRequest()!
        guard case .ignored = state.consume(cancelled, connectionID: connection) else { throw Failure(message: "stale terminal completed next request") }
        guard case .unavailable = state.consume(receipt(next, type: "chat_turn_terminal", turn: turn), connectionID: connection) else { throw Failure(message: "terminal without acceptance accepted") }
        try check(state.active?.requestID == next.requestID, "unconfirmed receipt erased request identity")
        _ = state.consume(receipt(next, type: "chat_turn_accepted", turn: turn), connectionID: connection)
        var malformed = receipt(next, type: "chat_turn_terminal", turn: turn)
        var payload = malformed["payload"] as! [String: Any]; payload["durable"] = 1; malformed["payload"] = payload
        guard case .unavailable = state.consume(malformed, connectionID: connection) else { throw Failure(message: "numeric durability accepted") }
        payload["durable"] = true; payload["action_outcome"] = "success"; malformed["payload"] = payload
        guard case .unavailable = state.consume(malformed, connectionID: connection) else { throw Failure(message: "invented effect success accepted") }
        for outcome in NativeChatTurnWire.outcomes {
            let data = receipt(next, type: "chat_turn_terminal", turn: turn, outcome: outcome)["payload"] as! [String: Any]
            try check(NativeChatTurnWire.terminal(data, reference: state.active!)?.outcome == outcome, "known outcome rejected")
        }
        print("PASS stale, malformed, noncanonical receipt boundaries and all processing outcomes")

        let old = state.active!
        state.configure(sessionID: "A", connectionID: UUID())
        let newerConnection = state.connectionID!
        _ = state.consume(capability(state), connectionID: newerConnection)
        let query = state.statusFrame(old)!
        try check(query["method"] as? String == "chat.status" && state.statusFrame(old) == nil && state.pendingAbortFrame() == nil, "reconnect acquired abort or replayed status")
        let recovered: [String: Any] = ["type": "res", "id": query["id"]!, "ok": true, "payload": ["found": true, "receipt": receipt(old, type: "chat_turn_terminal", turn: turn)["payload"]!]]
        guard case .terminal(let recoveredResult) = state.consume(recovered, connectionID: newerConnection) else { throw Failure(message: "read-only exact status failed") }
        try check(recoveredResult.reference.requestID == old.requestID && recoveredResult.replayed && state.active == nil, "status restored execution authority")
        try check(NativeTurnReference.restored(old.record, sessionID: "B") == nil, "foreign persisted reference restored")
        var invalid = old.record; invalid["request_id"] = old.requestID.uppercased()
        try check(NativeTurnReference.restored(invalid, sessionID: "A") == nil, "noncanonical persisted UUID restored")
        let expiring = state.statusFrame(old)!
        try check(!state.expireStatus(UUID().uuidString.lowercased()) && state.statusPending, "foreign status deadline cleared current read")
        try check(state.expireStatus(expiring["id"] as! String) && !state.statusPending && state.active == nil, "exact status deadline did not clear only its passive read")
        let retryRead = state.statusFrame(old)!
        try check(retryRead["id"] as? String != expiring["id"] as? String && state.pendingAbortFrame() == nil, "explicit later status read reused execution or abort authority")
        var uncertain = receipt(old, type: "chat_turn_terminal", turn: turn)["payload"] as! [String: Any]
        uncertain["action_outcome"] = "unknown"
        try check(NativeChatTurnWire.terminal(uncertain, reference: old)?.summary.contains("actions may still need checking") == true, "processing completion hid uncertain action outcome")
        print("PASS read-only reconciliation on a new connection without task or abort replay")
        let checkpoint = NativeTurnContextCheckpoint(sessionID: "A", generation: UUID().uuidString.lowercased(), revision: 3, attemptID: UUID().uuidString.lowercased())
        var committed = receipt(old, type: "chat_turn_terminal", turn: turn)["payload"] as! [String: Any]
        committed["context_checkpoint"] = checkpoint.record
        try check(NativeChatTurnWire.terminal(committed, reference: old)?.contextCheckpoint == checkpoint, "exact committed receipt not preserved")
        for badRevision: Any in [true, 3.0, "3", 0, -1, UInt64.max] {
            var row = checkpoint.record; row["revision"] = badRevision; committed["context_checkpoint"] = row
            try check(NativeChatTurnWire.terminal(committed, reference: old) == nil, "noncanonical committed revision accepted")
        }
        var wrong = checkpoint.record; wrong["extra"] = true; committed["context_checkpoint"] = wrong
        try check(NativeChatTurnWire.terminal(committed, reference: old) == nil, "unknown committed receipt fields accepted")
        committed["context_checkpoint"] = checkpoint.record; committed["processing_outcome"] = "cancelled"
        try check(NativeChatTurnWire.terminal(committed, reference: old) == nil, "unverified outcome certified a committed context")
        for advertised: [Any] in [[1], [true], [1.0], ["1"], [1, true], []] {
            var negotiated = NativeChatTurnState(); let connection = UUID()
            negotiated.configure(sessionID: "A", connectionID: connection)
            var frame = capability(negotiated); var payload = frame["payload"] as! [String: Any]
            payload["managed_chained_voice_versions"] = advertised; frame["payload"] = payload
            _ = negotiated.consume(frame, connectionID: connection)
            try check(negotiated.managedChainedVoiceReady == (advertised.count == 1 && NativeChatTurnWire.version(advertised.first)), "managed voice capability malformed or downgraded")
        }
        print("PASS committed context schema and strict managed voice capability negotiation")
        var refreshed = NativeChatTurnState(); let refreshConnection = UUID()
        refreshed.configure(sessionID: "A", connectionID: refreshConnection)
        var initial = capability(refreshed); var initialPayload = initial["payload"] as! [String: Any]
        initialPayload["managed_chained_voice_versions"] = [1]; initial["payload"] = initialPayload
        _ = refreshed.consume(initial, connectionID: refreshConnection)
        var readiness: [String: Any] = ["session_id": "A", "context_managed": true, "context_ready": false, "context_state": "in_progress", "managed_chained_voice_versions": [Any]()]
        refreshed.refreshManagedVoiceCapabilities(readiness)
        try check(refreshed.managedChainedVoiceReady, "temporary active-writer availability revoked negotiated voice")
        readiness["context_ready"] = true; readiness["context_state"] = "ready"; readiness["session_id"] = "B"
        refreshed.refreshManagedVoiceCapabilities(readiness)
        try check(refreshed.managedChainedVoiceReady, "foreign context revoked current voice capability")
        readiness["session_id"] = "A"; refreshed.refreshManagedVoiceCapabilities(readiness)
        try check(!refreshed.managedChainedVoiceReady && refreshed.ready, "correlated READY unsupported voice did not revoke admission independently of chat")
        readiness["managed_chained_voice_versions"] = [1]; refreshed.refreshManagedVoiceCapabilities(readiness)
        try check(refreshed.managedChainedVoiceReady, "fresh READY supported pipeline did not restore admission")
        readiness["managed_chained_voice_versions"] = [true]; refreshed.refreshManagedVoiceCapabilities(readiness)
        try check(!refreshed.managedChainedVoiceReady, "malformed READY version restored microphone admission")
        let stopPayload: [String: Any] = ["mode": "disabled", "managed_chained_voice_version": 1, "voice_attempt_version": 1, "voice_attempt_id": UUID().uuidString.lowercased()]
        let stopped: [String: Any] = ["type": "voice_config", "session_id": "A", "payload": stopPayload]
        try check(NativeChatTurnWire.managedVoiceStop(stopped), "captured versioned disable lost stop-only classification")
        for (key, value): (String, Any) in [("mode", "chained"), ("managed_chained_voice_version", true), ("voice_attempt_version", 1.0), ("voice_attempt_id", "invalid")] {
            var invalid = stopPayload; invalid[key] = value
            try check(!NativeChatTurnWire.managedVoiceStop(["type": "voice_config", "payload": invalid]), "malformed or start frame obtained stop-only admission")
        }
        try check(!NativeChatTurnWire.managedVoiceStop(["type": "audio_chunk", "payload": stopPayload]), "media obtained pending-context stop exception")
        print("PASS correlated READY capability revocation/restoration, active writer preservation and strict stop-only dispatch")
        print("NATIVE_CHAT_TURN_TESTS_PASSED: receipt state-machine fixtures only, no network or action execution")
    }
}
