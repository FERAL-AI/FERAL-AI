// Protocol fixtures only: no microphone, account, provider or personal profile.
import Foundation

private struct ManagedFailure: Error { let message: String }
private var checks = 0
private func expect(_ value: @autoclosure () -> Bool, _ message: String) throws {
    checks += 1; if !value() { throw ManagedFailure(message: message) }
}
@MainActor private final class ManagedAudio: NativeVoiceAudioIO {
    var starts = 0, stops = 0, plays = 0, shutdowns = 0
    var capture: ((Data) -> Void)?
    var blockFinish = false
    var finishBarrier: CheckedContinuation<Void, Never>?
    func authorize() async -> Bool { true }
    func startCapture(onPCM: @escaping (Data) -> Void, onFailure: @escaping (String) -> Void) throws { starts += 1; capture = onPCM }
    func stopCapture() { stops += 1; capture = nil }
    func finishCapture() async {
        if blockFinish { await withCheckedContinuation { finishBarrier = $0 } }
        stopCapture()
    }
    func play(_ data: Data, encoding: String, sampleRate: Double, completed: @escaping () -> Void) throws { plays += 1 }
    func clearPlayback() {}
    func shutdown() { shutdowns += 1; capture = nil }
}
@MainActor private final class ManagedFixture {
    let audio = ManagedAudio()
    let generation = UUID().uuidString.lowercased()
    let request = NativeTurnReference(sessionID: "managed-thread", requestID: UUID().uuidString.lowercased())
    let turn = UUID().uuidString.lowercased()
    let contextAttempt = UUID().uuidString.lowercased()
    var frames: [[String: Any]] = []
    var saved = true, begun = 0, stopped = 0, unknown = 0
    var transcript = ""
    var blockSave = false, refuseSend = false
    var saveBarrier: CheckedContinuation<NativeTurnReference?, Never>?
    lazy var engine = NativeVoiceEngine(sendFrame: { [weak self] frame in
        guard let self else { return }
        if self.refuseSend { throw ManagedFailure(message: "fixture transport refusal") }
        self.frames.append(frame)
    }, audio: audio)
    func setup() {
        engine.configureManaged(enabled: true, generation: generation, revision: 4, beginRequest: { [weak self] in
            guard let self else { return nil }; self.begun += 1
            if self.blockSave { return await withCheckedContinuation { self.saveBarrier = $0 } }
            return self.saved ? self.request : nil
        }, transcript: { [weak self] reference, text in
            if reference.requestID == self?.request.requestID { self?.transcript = text }
        }, stopTask: { [weak self] in self?.stopped += 1 }, unknown: { [weak self] _ in self?.unknown += 1 })
        engine.configureConnection(sessionID: "managed-thread", connected: true)
    }
    var attempt: [String: Any] {
        let payload = frames.first { $0["type"] as? String == "voice_config" }?["payload"] as? [String: Any] ?? [:]
        return payload.filter { ["voice_attempt_version", "voice_attempt_id", "managed_chained_voice_version"].contains($0.key) }
    }
    func checkpoint(_ revision: Int64 = 4, generation: String? = nil) -> NativeTurnContextCheckpoint {
        NativeTurnContextCheckpoint(sessionID: request.sessionID, generation: generation ?? self.generation, revision: revision, attemptID: contextAttempt)
    }
    func frame(_ type: String, _ additions: [String: Any] = [:], revision: Int64 = 4) -> [String: Any] {
        var payload = attempt
        payload["request_id"] = request.requestID; payload["context_checkpoint"] = checkpoint(revision).record
        payload.merge(additions) { _, value in value }
        return ["type": type, "session_id": request.sessionID, "payload": payload]
    }
    func start() async { await engine.start(mode: "chained", provider: "configured") }
    func configured() async { await engine.handle(frame: frame("voice_config_ack", ["status": "configured", "mode": "chained", "provider": "configured"])) }
    func admitted() async { await engine.handle(frame: frame("voice_utterance_begin_ack", ["status": "collecting"])) }
    func accepted() { engine.accepted(NativeTurnReference(sessionID: request.sessionID, requestID: request.requestID, turnID: turn)) }
    func terminal(replayed: Bool = false, revision: Int64 = 6, generation: String? = nil, outcome: String = "completed") {
        engine.terminal(NativeTurnTerminal(reference: NativeTurnReference(sessionID: request.sessionID, requestID: request.requestID, turnID: turn), outcome: outcome, text: "Done", actionOutcome: "not_asserted", approvals: [], replayed: replayed, contextCheckpoint: checkpoint(revision, generation: generation)))
    }
    func audioFrame(final: Bool = false, index: Int = 0, request: String? = nil, revision: Int64 = 6) -> [String: Any] {
        frame("audio_chunk", ["request_id": request ?? self.request.requestID, "turn_id": turn,
            "data_b64": final ? "" : NativeVoicePCM.encode([0.2]).base64EncodedString(), "encoding": "pcm16", "sample_rate": 24000,
            "channels": 1, "chunk_index": index, "is_final": final], revision: revision)
    }
    func count(_ type: String) -> Int { frames.filter { $0["type"] as? String == type }.count }
}
@main struct NativeManagedVoiceTests {
    @MainActor static func settle() async { for _ in 0..<30 { await Task.yield() } }
    @MainActor static func admission() async throws {
        let f = ManagedFixture(); f.setup(); await f.start()
        try expect(f.audio.starts == 0 && f.begun == 0, "configuration send admits no microphone or utterance")
        let config = f.frames[0]["payload"] as! [String: Any]
        try expect(config["managed_chained_voice_version"] as? Int == 1 && config["context_revision"] as? Int64 == 4 && config["context_generation"] as? String == f.generation, "config carries reviewed managed context")
        await f.configured()
        try expect(f.begun == 1 && f.count("voice_utterance_begin") == 1 && f.audio.starts == 0, "configured ACK only persists and requests admission")
        await f.engine.handle(frame: f.frame("voice_utterance_begin_ack", ["status": "collecting", "request_id": UUID().uuidString.lowercased()]))
        await f.engine.handle(frame: f.frame("voice_utterance_begin_ack", ["status": "collecting", "voice_attempt_version": true]))
        await f.engine.handle(frame: f.frame("voice_utterance_begin_ack", ["status": "collecting"], revision: 6))
        try expect(f.audio.starts == 0, "wrong request, malformed attempt or changed checkpoint cannot admit capture")
        await f.admitted(); await f.admitted()
        try expect(f.audio.starts == 1 && f.engine.canFinishSpeaking, "only matching collecting ACK starts microphone once")
        f.engine.receivePCM(Data(count: 5800)); await settle(); await f.engine.finishSpeaking()
        let chunks = f.frames.filter { $0["type"] as? String == "audio_chunk" }
        try expect(chunks.count == 2, "finish drains full PCM and trailing partial buffer")
        try expect((chunks[0]["payload"] as? [String: Any])?["chunk_index"] as? Int == 0 && (chunks[1]["payload"] as? [String: Any])?["chunk_index"] as? Int == 1, "ingress stays ordered")
        try expect(f.frames.last?["type"] as? String == "voice_utterance_finish" && f.audio.stops == 1 && f.stopped == 0, "finish stops only capture and sends finish after PCM")
        let count = f.frames.count; f.engine.receivePCM(Data(count: 4800)); await f.engine.finishSpeaking(); await settle()
        try expect(f.frames.count == count && f.begun == 1, "finished utterance cannot queue audio, duplicate finish or next request")
        await f.engine.stop()
    }
    @MainActor static func persistence() async throws {
        let f = ManagedFixture(); f.setup(); f.blockSave = true; await f.start()
        let ack = Task { await f.configured() }; await settle()
        try expect(f.engine.state == "saving_request" && f.count("voice_utterance_begin") == 0 && f.audio.starts == 0, "persistence barrier precedes begin and capture")
        await f.engine.stop(); f.saveBarrier?.resume(returning: f.request); await ack.value
        try expect(f.count("voice_utterance_begin") == 0 && f.audio.starts == 0, "late saved request after Stop cannot admit voice")
        let failed = ManagedFixture(); failed.setup(); failed.saved = false; await failed.start(); await failed.configured()
        try expect(failed.engine.state == "ended" && failed.audio.starts == 0 && failed.count("voice_utterance_begin") == 0, "failed save/readback sends no utterance")
        let realtime = ManagedFixture(); realtime.setup(); await realtime.engine.start(mode: "realtime", provider: "openai")
        try expect(realtime.frames.isEmpty && realtime.audio.starts == 0, "managed realtime remains refused")
        let lost = ManagedFixture(); lost.setup(); await lost.start(); lost.refuseSend = true; await lost.configured()
        try expect(lost.engine.state == "uncertain" && lost.unknown == 1 && lost.audio.starts == 0, "lost admission send exposes status reconciliation without retry")
        await lost.engine.stop()
    }
    @MainActor static func captureDrain() async throws {
        let f = ManagedFixture(); f.setup(); await f.start(); await f.configured(); await f.admitted()
        f.audio.blockFinish = true
        let finish = Task { await f.engine.finishSpeaking() }; await settle()
        try expect(f.count("voice_utterance_finish") == 0, "explicit finish waits for admitted microphone deliveries")
        f.audio.capture?(Data(count: 1000)); await settle()
        f.audio.finishBarrier?.resume(); await finish.value
        try expect(f.count("audio_chunk") == 1 && f.frames.last?["type"] as? String == "voice_utterance_finish", "last admitted delivery drains before final request")
        await f.engine.stop()
        let cancelled = ManagedFixture(); cancelled.setup(); await cancelled.start(); await cancelled.configured(); await cancelled.admitted()
        cancelled.audio.blockFinish = true
        let pending = Task { await cancelled.engine.finishSpeaking() }; await settle()
        await cancelled.engine.stop(); cancelled.audio.finishBarrier?.resume(); await pending.value
        try expect(cancelled.count("voice_utterance_finish") == 0 && cancelled.stopped == 1, "Stop during microphone drain cannot submit late finish")
    }
    @MainActor static func contextRevocation() async throws {
        for advance in [false, true] {
            let f = ManagedFixture(); f.setup(); await f.start(); await f.configured(); await f.admitted(); await f.engine.finishSpeaking(); f.accepted(); f.terminal()
            await f.engine.handle(frame: f.audioFrame())
            try expect(f.audio.plays == 1, "live terminal speech begins before context replacement")
            f.engine.observeManagedContext(generation: advance ? f.generation : UUID().uuidString.lowercased(), revision: advance ? 8 : 6)
            await f.engine.handle(frame: f.audioFrame(index: 1)); await settle()
            try expect(f.audio.plays == 1 && !f.engine.assistantSpeaking && f.engine.state == "uncertain", "verified context generation/revision replacement revokes queued speech")
            try expect(f.begun == 1 && f.stopped == 0, "context replacement never replays or claims task cancellation")
            await f.engine.stop()
        }
        let admitting = ManagedFixture(); admitting.setup(); await admitting.start(); await admitting.configured()
        admitting.engine.observeManagedContext(generation: admitting.generation, revision: 6)
        await admitting.admitted(); await settle()
        try expect(admitting.audio.starts == 0 && admitting.unknown == 1, "advanced context before collecting ACK cannot admit stale microphone")
        await admitting.engine.stop()
        let expected = ManagedFixture(); expected.setup(); await expected.start(); await expected.configured(); await expected.admitted(); await expected.engine.finishSpeaking(); expected.accepted(); expected.terminal()
        expected.engine.observeManagedContext(generation: expected.generation, revision: 6)
        await expected.engine.handle(frame: expected.audioFrame())
        try expect(expected.audio.plays == 1, "matching current READY commit preserves live authorized speech")
        await expected.engine.stop()
    }
    @MainActor static func exactACKAndState() async throws {
        let f = ManagedFixture(); f.setup(); await f.start(); await f.configured(); await f.admitted(); await f.engine.finishSpeaking()
        var finish = f.attempt; finish["request_id"] = f.request.requestID; finish["status"] = "submitted_processing"; finish["task_accepted"] = false
        await f.engine.handle(frame: ["type": "voice_utterance_finish_ack", "session_id": f.request.sessionID, "payload": finish])
        try expect(f.engine.diagnostic == "Voice submitted; waiting for the tracked task receipt." && f.engine.state == "processing", "actual finish ACK without checkpoint conveys submission only")
        finish["task_accepted"] = true
        await f.engine.handle(frame: ["type": "voice_utterance_finish_ack", "session_id": f.request.sessionID, "payload": finish])
        try expect(f.engine.state == "processing" && f.audio.plays == 0, "finish ACK cannot certify task acceptance or speech")
        await f.engine.handle(frame: f.frame("voice_state", ["state": "error", "request_id": UUID().uuidString.lowercased()]))
        await f.engine.handle(frame: f.frame("voice_state", ["state": "error"], revision: 8))
        try expect(f.engine.phase == "processing" && f.unknown == 0, "foreign or stale same-attempt voice state cannot alter task phase")
        await f.engine.handle(frame: f.frame("voice_state", ["state": "processing"]))
        try expect(f.engine.phase == "processing", "exact preterminal voice state is accepted")
        f.accepted(); f.terminal()
        await f.engine.handle(frame: f.frame("voice_state", ["state": "speaking", "turn_id": f.turn], revision: 6))
        try expect(f.engine.phase == "speaking", "exact authorized terminal checkpoint state is accepted")
        await f.engine.handle(frame: f.frame("voice_state", ["state": "error", "turn_id": f.turn], revision: 6))
        try expect(f.engine.state == "uncertain" && f.unknown == 1 && f.stopped == 0 && !f.engine.assistantSpeaking, "bound voice error retires media without claiming task cancellation")
        await f.engine.stop()
        for begin in [true, false] {
            let negative = ManagedFixture(); negative.setup(); await negative.start(); await negative.configured()
            if !begin { await negative.admitted(); await negative.engine.finishSpeaking() }
            var payload = negative.attempt
            payload["status"] = "error"; payload["code"] = "managed_voice_context_busy"; payload["retry_safe"] = false
            payload["request_id"] = UUID().uuidString.lowercased()
            let type = begin ? "voice_utterance_begin_ack" : "voice_utterance_finish_ack"
            await negative.engine.handle(frame: ["type": type, "session_id": negative.request.sessionID, "payload": payload])
            try expect(negative.unknown == 0, "foreign negative utterance ACK is ignored")
            payload["request_id"] = negative.request.requestID
            await negative.engine.handle(frame: ["type": type, "session_id": negative.request.sessionID, "payload": payload])
            try expect(negative.engine.state == "uncertain" && negative.unknown == 1 && negative.stopped == 0 && negative.audio.plays == 0, "bound negative utterance ACK requests reconciliation without replay")
            await negative.engine.stop()
        }
        let high = ManagedFixture(); high.setup()
        high.engine.configureManaged(enabled: true, generation: high.generation, revision: Int64.max - 1,
            beginRequest: { high.request }, transcript: { _, _ in }, stopTask: {}, unknown: { _ in })
        await high.start()
        await high.engine.handle(frame: high.frame("voice_config_ack", ["status": "configured", "mode": "chained", "provider": "configured"], revision: Int64.max - 1))
        await high.engine.handle(frame: high.frame("voice_utterance_begin_ack", ["status": "collecting"], revision: Int64.max - 1))
        try expect(high.audio.starts == 1, "canonical high checkpoint is admitted only by exact collecting ACK")
        await high.engine.finishSpeaking(); high.accepted()
        high.terminal(revision: Int64.max - 1)
        await high.engine.handle(frame: high.audioFrame(revision: Int64.max - 1))
        try expect(high.audio.plays == 0 && high.engine.state == "completed", "high original revision plus two cannot trap or authorize speech")
        await high.engine.stop()
    }
    @MainActor static func playback() async throws {
        let f = ManagedFixture(); f.setup(); await f.start(); await f.configured(); await f.admitted(); await f.engine.finishSpeaking(); f.accepted()
        await f.engine.handle(frame: f.audioFrame())
        try expect(f.audio.plays == 0, "speech before live committed terminal is refused")
        await f.engine.handle(frame: f.frame("transcript", ["role": "user", "text": "A saved request", "is_partial": false]))
        try expect(f.transcript == "A saved request", "exact recognition updates placeholder without issuing another task")
        f.terminal()
        await f.engine.handle(frame: f.audioFrame(request: UUID().uuidString.lowercased()))
        await f.engine.handle(frame: f.audioFrame(revision: 7))
        try expect(f.audio.plays == 0, "wrong task or committed checkpoint cannot play")
        await f.engine.handle(frame: f.audioFrame())
        try expect(f.audio.plays == 1 && f.engine.assistantSpeaking, "exact committed speech plays after capture stopped")
        await f.engine.interrupt()
        try expect(f.stopped == 0 && f.count("voice_interrupt") == 1, "speech interruption never cancels tracked task")
        await f.engine.handle(frame: f.audioFrame(index: 1))
        try expect(f.audio.plays == 1, "late speech after interruption never resumes")
        await f.engine.stop()
        try expect(f.stopped == 1, "task Stop routes once through exact chat cancellation")
        for mode in ["replayed", "generation", "revision", "failed"] {
            let rejected = ManagedFixture(); rejected.setup(); await rejected.start(); await rejected.configured(); await rejected.admitted(); await rejected.engine.finishSpeaking(); rejected.accepted()
            rejected.terminal(replayed: mode == "replayed", revision: mode == "revision" ? 8 : 6,
                              generation: mode == "generation" ? UUID().uuidString.lowercased() : nil, outcome: mode == "failed" ? "failed" : "completed")
            await rejected.engine.handle(frame: rejected.audioFrame())
            try expect(rejected.audio.plays == 0, "\(mode) terminal cannot authorize playback")
            await rejected.engine.stop()
        }
        let closed = ManagedFixture(); closed.setup(); await closed.start(); await closed.configured(); await closed.admitted(); await closed.engine.finishSpeaking(); closed.accepted(); closed.terminal()
        await closed.engine.handle(frame: closed.audioFrame(final: true)); await closed.engine.handle(frame: closed.audioFrame())
        try expect(closed.audio.plays == 0, "final speech sentinel closes playback authority against replay")
        closed.engine.configureConnection(sessionID: "other", connected: true)
        await closed.engine.handle(frame: closed.audioFrame())
        try expect(closed.audio.plays == 0 && closed.engine.state == "off", "thread replacement removes speech and task identity")
    }
    @MainActor static func main() async throws {
        try await admission(); try await persistence(); try await captureDrain(); try await contextRevocation(); try await exactACKAndState(); try await playback()
        print("PASS managed native voice: \(checks) assertions; no physical audio or provider access")
    }
}
