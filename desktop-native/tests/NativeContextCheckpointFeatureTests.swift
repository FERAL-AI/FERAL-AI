import Foundation
import Darwin

private func expect(_ value: @autoclosure () -> Bool, _ message: String) {
    guard value() else { fatalError(message) }
}
private func ready(_ sid: String = "context-fixture", revision: Any = 1,
                   generation: String = "a55f2162-52c4-42a0-8c0c-c4f77b867bdb") -> [String: Any] {
    ["session_id": sid, "context_managed": true, "context_checkpoint_versions": [1],
     "context_ready": true, "context_state": "ready",
     "managed_unsupported_paths": Array(NativeContextCheckpointWire.unsupportedPaths),
     "context_checkpoint": ["contract_version": 1, "session_id": sid, "generation": generation,
        "revision": revision, "durable": true, "initialized": true,
        "omissions": ["history_rows": 0, "system_rows": 0, "images": 0, "working_rows": 0]]]
}
@main struct NativeContextCheckpointFeatureTests {
    static func journalTests(_ fence: NativeContextRecoveryFence, other: NativeContextRecoveryFence,
                             defaults: UserDefaults) throws {
        let root = URL(fileURLWithPath: "/private/tmp/feral-context-journal-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: root) }
        let journal = root.appendingPathComponent("recovery.json")
        let store = NativeContextCheckpointPreferences(defaults: defaults, recoveryJournalURL: journal)
        let absent = try store.pendingRecovery(session: fence.sessionID, primary: "installation-A")
        expect(absent == nil && !FileManager.default.fileExists(atPath: journal.path), "missing journal is not invented")
        try store.rememberRecovery(fence, primary: "installation-A")
        let fresh = NativeContextCheckpointPreferences(defaults: defaults, recoveryJournalURL: journal)
        let retained = try fresh.pendingRecovery(session: fence.sessionID, primary: "installation-A")
        expect(retained == fence, "new store reads durable file rather than malformed UserDefaults recovery fixture")
        let attributes = try FileManager.default.attributesOfItem(atPath: journal.path)
        expect((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600, "journal has private file mode")
        let original = try Data(contentsOf: journal)
        func refuses(_ action: () throws -> Void, _ message: String) {
            var refused = false
            do { try action() } catch { refused = true }
            expect(refused, message)
        }
        refuses({ try fresh.clearRecovery(other, primary: "installation-A") }, "stale clear is refused under journal lock")
        refuses({ try fresh.rememberRecovery(other, primary: "installation-A") }, "new intent cannot overwrite unresolved recovery")
        let afterRefusal = try Data(contentsOf: journal)
        expect(afterRefusal == original, "refused CAS preserves journal bytes")
        let lock = Darwin.open(journal.path + ".lock", O_RDWR | O_NOFOLLOW)
        expect(lock >= 0 && flock(lock, LOCK_EX | LOCK_NB) == 0, "fixture holds adjacent journal lock")
        refuses({ try fresh.clearRecovery(fence, primary: "installation-A") }, "contended journal refuses without waiting")
        _ = flock(lock, LOCK_UN); Darwin.close(lock)
        try fresh.clearRecovery(fence, primary: "installation-A")
        let cleared = try store.pendingRecovery(session: fence.sessionID, primary: "installation-A")
        expect(cleared == nil, "matching clear is independently readable after atomic publication")
        try Data("malformed-private-fixture".utf8).write(to: journal)
        refuses({ _ = try fresh.pendingRecovery(session: fence.sessionID, primary: "installation-A") }, "malformed journal refused")
        let corrupt = try Data(contentsOf: journal)
        expect(corrupt == Data("malformed-private-fixture".utf8), "malformed journal is preserved")
        let dangling = root.appendingPathComponent("dangling.json")
        try FileManager.default.createSymbolicLink(at: dangling, withDestinationURL: root.appendingPathComponent("absent.json"))
        let redirected = NativeContextCheckpointPreferences(defaults: defaults, recoveryJournalURL: dangling)
        refuses({ try redirected.rememberRecovery(fence, primary: "installation-A") }, "dangling journal symlink is never replaced")
        let directoryLink = root.appendingPathComponent("linked-directory")
        try FileManager.default.createSymbolicLink(at: directoryLink, withDestinationURL: root)
        let ancestor = NativeContextCheckpointPreferences(defaults: defaults, recoveryJournalURL: directoryLink.appendingPathComponent("recovery.json"))
        refuses({ _ = try ancestor.pendingRecovery(session: fence.sessionID, primary: "installation-A") }, "symlink ancestor is refused")
        let fifo = root.appendingPathComponent("pipe.json")
        expect(mkfifo(fifo.path, 0o600) == 0, "create disposable FIFO fixture")
        let pipe = NativeContextCheckpointPreferences(defaults: defaults, recoveryJournalURL: fifo)
        refuses({ _ = try pipe.pendingRecovery(session: fence.sessionID, primary: "installation-A") }, "FIFO is refused without blocking")
        let alias = root.appendingPathComponent("alias.json")
        try FileManager.default.linkItem(at: journal, to: alias)
        refuses({ _ = try fresh.pendingRecovery(session: fence.sessionID, primary: "installation-A") }, "multiply-linked journal refused")
        try FileManager.default.removeItem(at: alias)
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: journal.path)
        refuses({ _ = try fresh.pendingRecovery(session: fence.sessionID, primary: "installation-A") }, "publicly readable journal refused")
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: journal.path)
        let oversized = try FileHandle(forWritingTo: journal)
        try oversized.truncate(atOffset: 8 * 1024 * 1024 + 1); try oversized.close()
        refuses({ _ = try fresh.pendingRecovery(session: fence.sessionID, primary: "installation-A") }, "oversized journal refused before reading")
        print("PASS durable private journal, restart, CAS, lock contention, corruption, symlink, FIFO, hard-link, mode and size refusals")
    }
    static func main() throws {
        let parsed = NativeContextCheckpointWire.parse(ready(), sessionID: "context-fixture", required: true)
        expect(parsed?.ready == true && parsed?.managed == true, "exact managed READY")
        expect(parsed?.checkpoint?.revision == 1, "actual initial revision one")
        var managedVoice = ready()
        managedVoice["managed_chained_voice_versions"] = [1]
        managedVoice["managed_unsupported_paths"] = Array(NativeContextCheckpointWire.unsupportedPaths.subtracting(["voice"]).union(["realtime_voice"]))
        expect(NativeContextCheckpointWire.parse(managedVoice, sessionID: "context-fixture", required: true)?.ready == true, "actual negotiated chained voice capability preserves READY")
        var unnegotiatedVoice = managedVoice; unnegotiatedVoice.removeValue(forKey: "managed_chained_voice_versions")
        expect(NativeContextCheckpointWire.parse(unnegotiatedVoice, sessionID: "context-fixture", required: true) == nil, "voice omission cannot imply capability")
        let invalidVoiceVersions: [[Any]] = [[true], [1.0], [2], [1, "1"]]
        for versions in invalidVoiceVersions {
            var invalidVoice = managedVoice; invalidVoice["managed_chained_voice_versions"] = versions
            expect(NativeContextCheckpointWire.parse(invalidVoice, sessionID: "context-fixture", required: true) == nil, "malformed or unsupported voice negotiation cannot remove refusal")
        }
        var realtimeUnqualified = managedVoice
        realtimeUnqualified["managed_unsupported_paths"] = Array(NativeContextCheckpointWire.unsupportedPaths.subtracting(["voice"]))
        expect(NativeContextCheckpointWire.parse(realtimeUnqualified, sessionID: "context-fixture", required: true) == nil, "chained negotiation never grants realtime support")
        for revision: Any in [true, 1.0, 0, -1, NSNumber(value: Int64.max), NSNumber(value: UInt64.max), "1"] {
            expect(NativeContextCheckpointWire.parse(ready(revision: revision), sessionID: "context-fixture", required: true) == nil, "invalid revision rejected")
        }
        expect(NativeContextCheckpointWire.parse(ready(revision: NSNumber(value: Int64.max - 1)), sessionID: "context-fixture", required: true)?.ready == true, "positive integer upper boundary")
        for generation in ["not-a-uuid", "A55F2162-52C4-42A0-8C0C-C4F77B867BDB", " a55f2162-52c4-42a0-8c0c-c4f77b867bdb"] {
            expect(NativeContextCheckpointWire.parse(ready(generation: generation), sessionID: "context-fixture", required: true) == nil, "canonical generation required")
        }
        expect(NativeContextCheckpointWire.parse(ready(), sessionID: "other", required: true) == nil, "wrong exact SID")
        for field in ["context_ready", "context_managed"] {
            var bad = ready(); bad[field] = 1
            expect(NativeContextCheckpointWire.parse(bad, sessionID: "context-fixture", required: true) == nil, "strict top-level Boolean")
        }
        for field in ["durable", "initialized"] {
            var bad = ready(); var checkpoint = bad["context_checkpoint"] as! [String: Any]; checkpoint[field] = 1; bad["context_checkpoint"] = checkpoint
            expect(NativeContextCheckpointWire.parse(bad, sessionID: "context-fixture", required: true) == nil, "strict checkpoint Boolean")
        }
        for count: Any in [true, 0.0, -1, "0", NSNumber(value: UInt64.max)] {
            var bad = ready(); var checkpoint = bad["context_checkpoint"] as! [String: Any]
            checkpoint["omissions"] = ["history_rows": count, "system_rows": 0, "images": 0, "working_rows": 0]; bad["context_checkpoint"] = checkpoint
            expect(NativeContextCheckpointWire.parse(bad, sessionID: "context-fixture", required: true) == nil, "strict omissions count")
        }
        var missing = ready(); var checkpoint = missing["context_checkpoint"] as! [String: Any]
        checkpoint["omissions"] = ["history_rows": 0, "system_rows": 0, "images": 0]; missing["context_checkpoint"] = checkpoint
        expect(NativeContextCheckpointWire.parse(missing, sessionID: "context-fixture", required: true) == nil, "all codec omission keys required")
        var blocked = ready(); blocked["context_ready"] = false; blocked["context_state"] = "in_progress"; blocked.removeValue(forKey: "context_checkpoint")
        expect(NativeContextCheckpointWire.parse(blocked, sessionID: "context-fixture", required: true)?.permitsSubmission == false, "in-progress blocks tasks")
        var contradiction = blocked; contradiction["context_checkpoint"] = ready()["context_checkpoint"]
        expect(NativeContextCheckpointWire.parse(contradiction, sessionID: "context-fixture", required: true) == nil, "non-ready cannot carry checkpoint")
        let legacy: [String: Any] = ["session_id": "context-fixture", "context_managed": false, "context_checkpoint_versions": [1], "context_ready": false, "context_state": "legacy"]
        expect(NativeContextCheckpointWire.parse(legacy, sessionID: "context-fixture", required: false)?.permitsSubmission == true, "ordinary chat preserved")
        expect(NativeContextCheckpointWire.parse(legacy, sessionID: "context-fixture", required: true) == nil, "known managed cannot downgrade")
        expect(NativeContextCheckpointWire.parse(["session_id": "context-fixture"], sessionID: "context-fixture", required: false)?.permitsSubmission == true, "older receipt-only runtime remains compatible")
        expect(NativeContextCheckpointWire.parse(["session_id": "context-fixture"], sessionID: "context-fixture", required: true) == nil, "older runtime cannot claim managed support")
        expect(NativeContextCheckpointWire.sessionQuery("context-fixture", required: false).count == 1, "ordinary websocket never opts in")
        expect(NativeContextCheckpointWire.sessionQuery("context-fixture", required: true).last?.value == "1", "explicit websocket opt-in before attach")
        var malformed = NativeContextCheckpointState()
        let malformedConnection = UUID()
        malformed.configure(sessionID: "context-fixture", connectionID: malformedConnection, required: false, requestID: "malformed")
        var broken = ready(); broken["context_ready"] = 1
        _ = malformed.consume(["type": "res", "id": "malformed", "ok": true, "payload": broken], connectionID: malformedConnection)
        expect(malformed.managed && !malformed.permitsSubmission && !malformed.canSaveDisplay, "positive managed flag survives malformed capability")
        malformed.configure(sessionID: "context-fixture", connectionID: UUID(), required: false, requestID: "retry")
        expect(malformed.managed, "known managed cannot opt out on reconnect")
        var refused = blocked; refused["context_managed"] = false; refused["context_state"] = "legacy_unavailable"
        expect(NativeContextCheckpointWire.parse(refused, sessionID: "context-fixture", required: true)?.message.contains("cannot be converted") == true, "actual refused attachment remains explained without claiming managed")
        expect(NativeContextCheckpointWire.parse(refused, sessionID: "context-fixture", required: false)?.permitsSubmission == false, "refused attachment cannot fall back to legacy dispatch")
        let connection = UUID()
        var state = NativeContextCheckpointState()
        state.configure(sessionID: "context-fixture", connectionID: connection, required: true, requestID: "capability")
        expect(!state.permitsSubmission && !state.canSaveDisplay, "no first save/send before READY")
        let response: [String: Any] = ["type": "res", "id": "capability", "ok": true, "payload": ready()]
        expect(!state.consume(response, connectionID: UUID()), "stale connection ignored")
        var wrong = response; wrong["id"] = "foreign"
        expect(!state.consume(wrong, connectionID: connection), "foreign request ignored")
        expect(state.consume(response, connectionID: connection), "initial exact capability consumed")
        expect(state.permitsSubmission && state.canSaveDisplay, "READY permits presave")
        let refresh = state.refreshFrame()!
        expect(!state.permitsSubmission && state.canSaveDisplay, "refresh blocks dispatch but preserves display marker save")
        expect(state.consume(["type": "res", "id": refresh["id"]!, "ok": true, "payload": blocked], connectionID: connection), "blocked refresh consumed")
        expect(state.managed && !state.permitsSubmission && state.canSaveDisplay, "cancelled checkpoint cannot become chat ready")
        let refreshed = state.refreshFrame()!
        _ = state.consume(["type": "res", "id": refreshed["id"]!, "ok": true, "payload": ready(revision: 2)], connectionID: connection)
        expect(state.permitsSubmission, "same generation advancing revision")
        let regressed = state.refreshFrame()!
        _ = state.consume(["type": "res", "id": regressed["id"]!, "ok": true, "payload": ready(revision: 1)], connectionID: connection)
        expect(state.capability?.state == "conflict" && !state.permitsSubmission, "revision regression blocks")
        let drifted = state.refreshFrame()!
        _ = state.consume(["type": "res", "id": drifted["id"]!, "ok": true, "payload": ready(revision: 3, generation: UUID().uuidString.lowercased())], connectionID: connection)
        expect(state.capability?.state == "conflict", "generation change cannot reset authority")
        state.configure(sessionID: "context-fixture", connectionID: UUID(), required: true, requestID: "reconnect")
        expect(!state.permitsSubmission && state.canSaveDisplay, "reconnect retains verified display boundary")
        state.expire("reconnect")
        expect(!state.permitsSubmission && state.managed, "timeout never selects legacy")
        let interruptedGeneration = "a55f2162-52c4-42a0-8c0c-c4f77b867bdb"
        let interruptionAttempt = "a99b4ddd-1ffc-4e53-bf24-548cd5f524f4"
        let recoveredGeneration = "fffeeeab-fd01-48dd-a5b3-82bdce780731"
        var recoverable = blocked
        recoverable["context_recovery_versions"] = [1]
        recoverable["context_recovery"] = ["contract_version": 1, "session_id": "context-fixture",
            "generation": interruptedGeneration, "revision": 2, "attempt_id": interruptionAttempt,
            "state": "in_progress", "durable": true, "requires_unknown_effects_acknowledgement": true]
        let fence = NativeContextRecoveryFence.parse(recoverable["context_recovery"], sessionID: "context-fixture")!
        expect(NativeContextCheckpointWire.parse(recoverable, sessionID: "context-fixture", required: true)?.recovery == fence, "exact interrupted fence parsed")
        for field in ["revision", "durable", "requires_unknown_effects_acknowledgement", "attempt_id", "generation", "session_id"] {
            var invalid = recoverable
            var pending = invalid["context_recovery"] as! [String: Any]
            pending[field] = field == "revision" ? true : "invalid"
            invalid["context_recovery"] = pending
            expect(NativeContextCheckpointWire.parse(invalid, sessionID: "context-fixture", required: true) == nil, "malformed recovery authority refused")
        }
        var recoveryState = NativeContextCheckpointState()
        recoveryState.configure(sessionID: "context-fixture", connectionID: connection, required: true, requestID: "initial")
        _ = recoveryState.consume(["type": "res", "id": "initial", "ok": true, "payload": ready()], connectionID: connection)
        let pendingRead = recoveryState.refreshFrame()!
        _ = recoveryState.consume(["type": "res", "id": pendingRead["id"]!, "ok": true, "payload": recoverable], connectionID: connection)
        expect(recoveryState.canRecover && !recoveryState.permitsSubmission, "interruption offers review without dispatch")
        expect(recoveryState.recoveryFrame(review: fence, connectionID: UUID()) == nil, "stale recovery connection rejected")
        let otherFence = NativeContextRecoveryFence(sessionID: fence.sessionID, generation: fence.generation, revision: 3, attemptID: fence.attemptID)
        expect(recoveryState.recoveryFrame(review: otherFence, connectionID: connection) == nil, "changed recovery review rejected")
        let recoveryFrame = recoveryState.recoveryFrame(review: fence, connectionID: connection)!
        expect(recoveryFrame["method"] as? String == "session.context.recover", "explicit recovery RPC")
        expect(NativeContextCheckpointWire.boolean((recoveryFrame["params"] as! [String: Any])["acknowledge_unknown_effects"]) == true, "unknown effects explicitly acknowledged")
        expect(recoveryState.recoveryFrame(review: fence, connectionID: connection) == nil && recoveryState.refreshFrame() == nil, "no concurrent recovery or capability read")
        var recoveredRecord = ready(revision: 3, generation: recoveredGeneration)["context_checkpoint"] as! [String: Any]
        recoveredRecord["attempt_id"] = "d10ad8cd-bf90-4401-a4ec-8f80732328b2"
        recoveredRecord["initialized"] = false
        let recoveryPayload: [String: Any] = ["contract_version": 1, "session_id": "context-fixture", "status": "recovered",
            "context_ready": true, "durable": true, "replayed": false, "action_outcome": "unknown", "context_checkpoint": recoveredRecord]
        let recoveryReply: [String: Any] = ["type": "res", "id": recoveryFrame["id"]!, "ok": true, "payload": recoveryPayload]
        expect(!recoveryState.consumeRecovery(recoveryReply, connectionID: UUID()), "foreign recovery receipt ignored")
        expect(recoveryState.consumeRecovery(recoveryReply, connectionID: connection), "exact recovery receipt accepted")
        expect(!recoveryState.permitsSubmission && !recoveryState.canRecover, "receipt alone cannot enable next task")
        let recoveryReadback = recoveryState.refreshFrame()!
        _ = recoveryState.consume(["type": "res", "id": recoveryReadback["id"]!, "ok": true, "payload": ready(revision: 3, generation: recoveredGeneration)], connectionID: connection)
        expect(!recoveryState.permitsSubmission && recoveryState.recoveryReconciledFence == fence, "readback cannot send before durable intent cleanup")
        recoveryState.finishRecoveryPersistence(fence)
        expect(recoveryState.permitsSubmission && recoveryState.recoveryFence == nil, "exact independent readback enables conversation")
        expect(recoveryState.recoveryMessage?.contains("effects remain uncertain") == true, "recovery never claims external outcomes")

        func uncertainRecovery() -> NativeContextCheckpointState {
            var fixture = NativeContextCheckpointState()
            fixture.configure(sessionID: "context-fixture", connectionID: connection, required: true, requestID: "pending")
            _ = fixture.consume(["type": "res", "id": "pending", "ok": true, "payload": recoverable], connectionID: connection)
            let sent = fixture.recoveryFrame(review: fence, connectionID: connection)!
            fixture.expireRecovery(sent["id"] as! String)
            return fixture
        }
        var unknownRecovery = uncertainRecovery()
        expect(unknownRecovery.canCheckRecovery && !unknownRecovery.canRecover && !unknownRecovery.permitsSubmission, "lost receipt retains only reconciliation authority")
        let statusFrame = unknownRecovery.recoveryFrame(review: fence, connectionID: connection)!
        expect(statusFrame["method"] as? String == "session.context.recoveryStatus" && (statusFrame["params"] as! [String: Any])["acknowledge_unknown_effects"] == nil, "lost receipt causes read-only status, never retry")
        var recoveredStatus = recoveryPayload; recoveredStatus["recovered"] = true
        _ = unknownRecovery.consumeRecovery(["type": "res", "id": statusFrame["id"]!, "ok": true, "payload": recoveredStatus], connectionID: connection)
        expect(!unknownRecovery.permitsSubmission, "read-only recovery status still requires capability readback")
        let statusReadback = unknownRecovery.refreshFrame()!
        _ = unknownRecovery.consume(["type": "res", "id": statusReadback["id"]!, "ok": true, "payload": ready(revision: 3, generation: recoveredGeneration)], connectionID: connection)
        unknownRecovery.finishRecoveryPersistence(fence)
        expect(unknownRecovery.permitsSubmission, "lost reply reconciles without recovering twice")
        for status in ["not_recovered", "superseded"] {
            var fixture = uncertainRecovery()
            let query = fixture.recoveryFrame(review: fence, connectionID: connection)!
            _ = fixture.consumeRecovery(["type": "res", "id": query["id"]!, "ok": true,
                "payload": ["contract_version": 1, "session_id": "context-fixture", "status": status,
                    "recovered": false, "context_ready": false, "durable": true, "replayed": false, "action_outcome": "unknown"]], connectionID: connection)
            expect(!fixture.permitsSubmission, "non-recovered status cannot enable tasks")
            if status == "not_recovered" {
                let refresh = fixture.refreshFrame()!
                _ = fixture.consume(["type": "res", "id": refresh["id"]!, "ok": true, "payload": recoverable], connectionID: connection)
                expect(fixture.canRecover, "confirmed no mutation permits fresh review")
            }
        }
        for field in ["replayed", "action_outcome", "durable", "context_ready"] {
            var fixture = uncertainRecovery()
            let query = fixture.recoveryFrame(review: fence, connectionID: connection)!
            var invalid = recoveredStatus; invalid[field] = field == "action_outcome" ? "success" : 1
            _ = fixture.consumeRecovery(["type": "res", "id": query["id"]!, "ok": true, "payload": invalid], connectionID: connection)
            expect(!fixture.permitsSubmission && fixture.canCheckRecovery, "malformed recovery status never grants authority")
        }
        var drift = uncertainRecovery()
        let query = drift.recoveryFrame(review: fence, connectionID: connection)!
        _ = drift.consumeRecovery(["type": "res", "id": query["id"]!, "ok": true, "payload": recoveredStatus], connectionID: connection)
        let driftRead = drift.refreshFrame()!
        _ = drift.consume(["type": "res", "id": driftRead["id"]!, "ok": true, "payload": ready(revision: 4, generation: recoveredGeneration)], connectionID: connection)
        expect(!drift.permitsSubmission && drift.capability?.state == "conflict", "changed recovery readback refused")
        drift.configure(sessionID: "other", connectionID: UUID(), required: false, requestID: "other")
        expect(drift.recoveryFence == nil && !drift.canCheckRecovery, "new thread cannot inherit recovery authority")
        var gate = NativeContextActionGate()
        let epoch = UUID()
        let policy = NativeSelectedContextPolicy(sessionID: "A", connectionID: epoch, taskReady: true, managed: false)
        expect(gate.update(policy), "policy adopted")
        let original = gate.revision
        expect(!gate.update(policy) && gate.accepts(original), "identical policy preserves review")
        for changed in [NativeSelectedContextPolicy(sessionID: "B", connectionID: epoch, taskReady: true, managed: false),
            NativeSelectedContextPolicy(sessionID: "A", connectionID: UUID(), taskReady: true, managed: false),
            NativeSelectedContextPolicy(sessionID: "A", connectionID: epoch, taskReady: false, managed: false),
            NativeSelectedContextPolicy(sessionID: "A", connectionID: epoch, taskReady: true, managed: true)] {
            _ = gate.update(policy); let reviewed = gate.revision; _ = gate.update(changed)
            expect(!gate.accepts(reviewed), "SID/connection/readiness/mode each invalidate reviews")
        }
        let suite = "ai.feral.tests.context." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let preferences = NativeContextCheckpointPreferences(defaults: defaults)
        try preferences.rememberRecovery(fence, primary: "installation-A")
        let retainedRecovery = try preferences.pendingRecovery(session: fence.sessionID, primary: "installation-A")
        let foreignRecovery = try preferences.pendingRecovery(session: fence.sessionID, primary: "installation-B")
        expect(retainedRecovery == fence, "pending recovery is durable per installation and session")
        expect(foreignRecovery == nil, "another installation cannot inherit recovery")
        var overwriteRefused = false
        do { try preferences.rememberRecovery(otherFence, primary: "installation-A") } catch { overwriteRefused = true }
        expect(overwriteRefused, "a different recovery intent cannot overwrite the outstanding one")
        var freshProcess = NativeContextCheckpointState()
        freshProcess.configure(sessionID: fence.sessionID, connectionID: connection, required: true, requestID: "fresh-process")
        freshProcess.restorePendingRecovery(try preferences.pendingRecovery(session: fence.sessionID, primary: "installation-A")!)
        _ = freshProcess.consume(["type": "res", "id": "fresh-process", "ok": true, "payload": ready(revision: 3, generation: recoveredGeneration)], connectionID: connection)
        expect(!freshProcess.permitsSubmission && freshProcess.canCheckRecovery, "restart cannot forget the lost recovery receipt even when runtime is ready")
        freshProcess.configure(sessionID: "other", connectionID: UUID(), required: false, requestID: "other")
        freshProcess.configure(sessionID: fence.sessionID, connectionID: connection, required: true, requestID: "returned")
        freshProcess.restorePendingRecovery(try preferences.pendingRecovery(session: fence.sessionID, primary: "installation-A")!)
        _ = freshProcess.consume(["type": "res", "id": "returned", "ok": true, "payload": ready(revision: 3, generation: recoveredGeneration)], connectionID: connection)
        expect(freshProcess.canCheckRecovery && !freshProcess.permitsSubmission, "switching away and back restores outstanding recovery")
        let captured = NativeContextRecoveryReview(fence: fence, connectionID: connection, selectionID: UUID())
        expect(captured != NativeContextRecoveryReview(fence: fence, connectionID: UUID(), selectionID: captured.selectionID), "same fence on a new connection requires a new review")
        expect(captured != NativeContextRecoveryReview(fence: fence, connectionID: connection, selectionID: UUID()), "same fence on a new selection requires a new review")
        var staleClearRefused = false
        do { try preferences.clearRecovery(otherFence, primary: "installation-A") } catch { staleClearRefused = true }
        let afterStaleClear = try preferences.pendingRecovery(session: fence.sessionID, primary: "installation-A")
        expect(staleClearRefused && afterStaleClear == fence, "stale readback cannot clear a newer saved intent")
        try preferences.clearRecovery(fence, primary: "installation-A")
        let afterMatchingClear = try preferences.pendingRecovery(session: fence.sessionID, primary: "installation-A")
        expect(afterMatchingClear == nil, "matching completed recovery clears only its own intent")
        defaults.set("malformed", forKey: "native.pendingContextRecovery.v1")
        var malformedRecoveryRefused = false
        do { _ = try preferences.pendingRecovery(session: fence.sessionID, primary: "installation-A") } catch { malformedRecoveryRefused = true }
        expect(malformedRecoveryRefused && defaults.string(forKey: "native.pendingContextRecovery.v1") == "malformed", "invalid saved intent is refused without reset")
        freshProcess.blockRecoveryPreferences()
        expect(!freshProcess.permitsSubmission, "unverified preference state cannot authorize a task")
        try preferences.remember("context-fixture", primary: "installation-A")
        expect(preferences.required("context-fixture", primary: "installation-A"), "opt-in mode retained")
        expect(!preferences.required("context-fixture", primary: "installation-B"), "mode scoped to verified installation")
        try preferences.setPending("context-fixture", primary: "installation-A")
        expect(preferences.pending(primary: "installation-A") == "context-fixture", "interrupted setup identity retained")
        try preferences.setPending(nil, primary: "installation-A")
        expect(preferences.pending(primary: "installation-A") == nil && preferences.required("context-fixture", primary: "installation-A"), "finishing setup never discards managed policy")
        defaults.set("corrupt-mode-fixture", forKey: "native.savedContextModes.v1")
        var invalidPreferences = false
        do { try preferences.remember("another-fixture", primary: "installation-A") } catch { invalidPreferences = true }
        expect(invalidPreferences && defaults.string(forKey: "native.savedContextModes.v1") == "corrupt-mode-fixture", "malformed mode preferences never reset implicitly")
        try journalTests(fence, other: otherFence, defaults: defaults)
        print("Native context checkpoint: strict wire, fencing, readiness, compatibility and isolated preferences passed")
    }
}
