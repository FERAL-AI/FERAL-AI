import Foundation

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
    static func main() throws {
        let parsed = NativeContextCheckpointWire.parse(ready(), sessionID: "context-fixture", required: true)
        expect(parsed?.ready == true && parsed?.managed == true, "exact managed READY")
        expect(parsed?.checkpoint?.revision == 1, "actual initial revision one")
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
        print("Native context checkpoint: strict wire, fencing, readiness, compatibility and isolated preferences passed")
    }
}
