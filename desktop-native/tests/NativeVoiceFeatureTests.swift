// Deterministic protocol/PCM fixtures. The mock never accesses microphones,
// audio output devices, TCC permissions, Keychain or a provider.
import Foundation
import AVFoundation

@MainActor private final class VoiceAudioFixture: NativeVoiceAudioIO {
    var allowed = true
    var authorizeCount = 0
    var startCount = 0
    var shutdownCount = 0
    var clearCount = 0
    var capture: ((Data) -> Void)?
    var completions: [() -> Void] = []
    var played: [(Data, String, Double)] = []
    func authorize() async -> Bool { authorizeCount += 1; return allowed }
    func startCapture(onPCM: @escaping (Data) -> Void, onFailure: @escaping (String) -> Void) throws { startCount += 1; capture = onPCM }
    func play(_ data: Data, encoding: String, sampleRate: Double, completed: @escaping () -> Void) throws { played.append((data, encoding, sampleRate)); completions.append(completed) }
    func clearPlayback() { clearCount += 1 }
    func shutdown() { shutdownCount += 1 }
}
@MainActor private final class VoiceWireFixture {
    var frames: [[String: Any]] = []
    var refuse = false
    func send(_ frame: [String: Any]) async throws { if refuse { throw TestFailure(message: "fixture send refused") }; frames.append(frame) }
    func count(_ type: String) -> Int { frames.filter { $0["type"] as? String == type }.count }
}
private struct TestFailure: Error { let message: String }
private var assertions = 0
private func check(_ condition: @autoclosure () -> Bool, _ message: String) throws { assertions += 1; if !condition() { throw TestFailure(message: message) } }

@main struct NativeVoiceFeatureTests {
    static func frame(_ type: String, _ payload: [String: Any], session: String = "fixture-session") -> [String: Any] { ["type": type, "session_id": session, "payload": payload] }
    @MainActor static func settle() async { for _ in 0..<20 { await Task.yield() } }
    @MainActor private static func make() -> (NativeVoiceEngine, VoiceAudioFixture, VoiceWireFixture) {
        let audio = VoiceAudioFixture(), wire = VoiceWireFixture()
        let engine = NativeVoiceEngine(sendFrame: { try await wire.send($0) }, audio: audio)
        engine.configureConnection(sessionID: "fixture-session", connected: true)
        return (engine, audio, wire)
    }
    @MainActor static func active(_ engine: NativeVoiceEngine) async {
        await engine.start(mode: "realtime", provider: "openai")
        await engine.handle(frame: frame("voice_config_ack", ["status": "ok", "mode": "realtime", "provider": "openai"]))
    }
    @MainActor static func main() async throws {
        let bytes = NativeVoicePCM.encode([-1, 0, 1, 2, -.infinity, .nan])
        try check(Array(bytes.prefix(6)) == [0, 128, 0, 0, 255, 127], "PCM wire is explicitly little endian and clips correctly")
        let samples = try NativeVoicePCM.decode(bytes)
        try check(samples[0] == -1 && samples[1] == 0 && samples[2] > 0.999 && samples[4] == 0 && samples[5] == 0, "PCM handles clipping and nonfinite input without undefined conversion")
        do { _ = try NativeVoicePCM.decode(Data([1])); throw TestFailure(message: "odd PCM accepted") } catch is TestFailure { throw TestFailure(message: "odd PCM accepted") } catch { assertions += 1 }
        let inputFormat = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 2)!
        let target = AVAudioFormat(standardFormatWithSampleRate: 24000, channels: 1)!
        let input = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: 4800)!; input.frameLength = 4800
        for channel in 0..<2 { for i in 0..<4800 { input.floatChannelData![channel][i] = 0.25 } }
        let converter = AVAudioConverter(from: inputFormat, to: target)!
        let converted = try NativeVoicePCM.convert(input, converter: converter)
        let convertedSamples = try NativeVoicePCM.decode(converted)
        try check(convertedSamples.count > 2000 && convertedSamples.count <= 2600 && convertedSamples.dropFirst(128).contains(where: {abs($0 - 0.25) < 0.02}), "AVAudioConverter resamples 48k stereo fixture to 24k mono without device access")
        let (beforeAck, beforeAckAudio, _) = make()
        await beforeAck.start(mode: "realtime", provider: "openai")
        await beforeAck.handle(frame: frame("audio_response", ["data_b64": "AAAAAA==", "encoding": "pcm16", "sample_rate": 24000]))
        await beforeAck.handle(frame: frame("audio_delta", ["data_b64": "AAAAAA==", "encoding": "pcm16", "sample_rate": 24000]))
        await beforeAck.handle(frame: frame("tts_chunk", ["data_b64": "AQID", "encoding": "mp3", "chunk_index": 0]))
        await beforeAck.handle(frame: frame("transcript", ["text": "late caption before configuration", "role": "assistant"]))
        await beforeAck.handle(frame: frame("voice_state", ["state": "speaking"]))
        try check(beforeAckAudio.played.isEmpty && beforeAck.transcripts.isEmpty && beforeAck.phase == "idle" && beforeAckAudio.startCount == 0, "media cannot play or alter captions before the configuration acknowledgment")
        await beforeAck.stop()
        let (unbound, unboundAudio, _) = make()
        await unbound.start(mode: "realtime", provider: "openai")
        await unbound.handle(frame: ["type": "voice_config_ack", "payload": ["status": "ok", "mode": "realtime", "provider": "openai"]])
        await unbound.handle(frame: ["type": "voice_config_ack", "session_id": 7, "payload": ["status": "ok", "mode": "realtime", "provider": "openai"]])
        try check(unbound.state == "starting" && unboundAudio.startCount == 0, "missing or malformed session identity cannot authorize capture")
        await unbound.stop()
        let (providerMismatch, providerMismatchAudio, _) = make()
        await providerMismatch.start(mode: "realtime", provider: "openai")
        await providerMismatch.handle(frame: frame("voice_config_ack", ["status": "ok", "mode": "realtime", "provider": "gemini"]))
        try check(providerMismatch.state == "ended" && providerMismatchAudio.startCount == 0, "same-mode acknowledgment from another requested provider cannot authorize capture")
        let (providerMissing, providerMissingAudio, _) = make()
        await providerMissing.start(mode: "realtime", provider: "openai")
        await providerMissing.handle(frame: frame("voice_config_ack", ["status": "ok", "mode": "realtime"]))
        try check(providerMissing.state == "ended" && providerMissingAudio.startCount == 0, "an acknowledgment without the requested provider cannot authorize capture")
        let (engine, audio, wire) = make()
        try check(audio.authorizeCount == 0 && audio.startCount == 0 && wire.frames.isEmpty, "initialization never requests microphone or provider access")
        await engine.start(mode: "realtime", provider: "openai")
        try check(engine.state == "starting" && audio.authorizeCount == 1 && audio.startCount == 0 && wire.count("voice_config") == 1, "explicit Start authorizes but does not capture before ack")
        engine.receivePCM(Data(count: 4800)); await settle()
        try check(wire.count("audio_chunk") == 0, "no microphone audio before configuration ack")
        await engine.handle(frame: frame("voice_config_ack", ["status": "ok", "mode": "realtime", "provider": "openai"], session: "other-session"))
        try check(audio.startCount == 0, "wrong conversation ack never starts capture")
        await engine.handle(frame: frame("voice_status", ["state": "degraded", "provider": "openai", "fallback_provider": "piper", "summary": "fixture fallback"]))
        await engine.handle(frame: frame("voice_config_ack", ["status": "ok", "mode": "realtime", "provider": "openai"]))
        try check(engine.state == "degraded" && audio.startCount == 1 && engine.acknowledgedProvider == "openai" && engine.fallbackProvider == "piper", "degraded status before ack remains truthful and still binds capture")
        engine.receivePCM(Data(count: 4800)); await settle()
        let chunk = wire.frames.first(where: {$0["type"] as? String == "audio_chunk"})!["payload"] as! [String: Any]
        try check(chunk["encoding"] as? String == "pcm16" && chunk["sample_rate"] as? Int == 24000 && chunk["channels"] as? Int == 1 && chunk["chunk_index"] as? Int == 0 && chunk["is_final"] as? Bool == false && Data(base64Encoded: chunk["data_b64"] as! String)?.count == 4800, "actual silence chunks keep exact continuous server-VAD wire schema")
        await engine.setMuted(true); engine.receivePCM(Data(count: 4800)); await settle()
        try check(engine.muted && wire.count("voice_mute") == 1 && wire.count("audio_chunk") == 1, "mute disables ingress and explicitly signals server")
        await engine.setMuted(false); engine.receivePCM(Data(count: 4800)); await settle()
        try check(!engine.muted && wire.count("audio_chunk") == 2, "explicit unmute resumes capture")
        await engine.handle(frame: frame("transcript", ["item_id": "utterance", "text": "hel", "role": "user", "is_partial": true, "confidence": 0.4, "seq": 1]))
        await engine.handle(frame: frame("transcript", ["item_id": "utterance", "text": "hello", "role": "user", "is_partial": false, "confidence": 0.9, "seq": 2]))
        await engine.handle(frame: frame("transcript", ["item_id": "utterance", "text": "older", "role": "user", "is_partial": true, "seq": 0]))
        try check(engine.transcripts.count == 1 && engine.transcripts[0].text == "hello" && !engine.transcripts[0].partial && engine.transcripts[0].confidence == 0.9, "ordered partial/final captions preserve provider metadata")
        let pcm = NativeVoicePCM.encode([0.2, 0.1]).base64EncodedString()
        await engine.handle(frame: frame("audio_response", ["data_b64": pcm, "encoding": "pcm16"], session: "other-session"))
        await engine.handle(frame: ["type": "audio_response", "payload": ["data_b64": pcm, "encoding": "pcm16"]])
        await engine.handle(frame: ["type": "transcript", "session_id": 7, "payload": ["text": "unbound caption", "role": "assistant"]])
        try check(audio.played.isEmpty && engine.transcripts.count == 1, "active media still requires an exact string session identity")
        await engine.handle(frame: frame("audio_response", ["data_b64": pcm, "encoding": "pcm16", "sample_rate": 24000, "is_final": true]))
        try check(audio.played.count == 1 && engine.assistantSpeaking, "final audio bytes still play; speaking reflects unfinished playback")
        audio.completions.removeFirst()()
        try check(!engine.assistantSpeaking, "speaking clears after actual playback completion")
        await engine.handle(frame: frame("tts_chunk", ["data_b64": "AQID", "encoding": "mp3", "chunk_index": 0, "is_final": false]))
        await engine.handle(frame: frame("tts_chunk", ["data_b64": "AQID", "encoding": "mp3", "chunk_index": 0, "is_final": false]))
        try check(audio.played.count == 2 && engine.error?.contains("out-of-order") == true, "duplicate TTS frames are refused rather than replayed")
        await engine.handle(frame: frame("audio_delta", ["data_b64": pcm, "sample_rate": 24000]))
        let oldPlayback = audio.completions.last!
        await engine.interrupt()
        try check(!engine.assistantSpeaking && wire.count("voice_interrupt") == 1, "barge-in clears output and sends actual interruption request")
        await engine.handle(frame: frame("voice_interrupt_ack", ["status": "requested", "cancel_requested": true, "session_preserved": true]))
        try check(engine.diagnostic?.contains("does not confirm") == true, "requested cancellation never promises remote audio disappeared")
        oldPlayback(); try check(!engine.assistantSpeaking, "late playback completion cannot restore interrupted audio state")
        await engine.handle(frame: frame("speech_started", [:]))
        try check(engine.phase == "listening" && !engine.assistantSpeaking, "server speech-start flushes output")
        await engine.handle(frame: frame("audio_response", ["data_b64": "AQ==", "encoding": "pcm16"]))
        try check(engine.error?.contains("incomplete") == true, "malformed response PCM is visible")
        let oldCapture = audio.capture!
        let beforeStop = wire.count("audio_chunk"); await engine.stop(); oldCapture(Data(count: 4800)); await settle()
        try check(engine.state == "off" && wire.count("audio_chunk") == beforeStop && (wire.frames.last?["payload"] as? [String: Any])?["mode"] as? String == "disabled", "stop shuts devices, disables server voice and ignores old tap callbacks")
        let playedBeforeLate = audio.played.count, captionsBeforeLate = engine.transcripts.count
        await engine.handle(frame: frame("audio_response", ["data_b64": pcm, "encoding": "pcm16"]))
        await engine.handle(frame: frame("transcript", ["text": "after stop", "role": "assistant"]))
        try check(audio.played.count == playedBeforeLate && engine.transcripts.count == captionsBeforeLate && engine.state == "off", "stopped voice ignores late incoming media and captions")
        await active(engine)
        engine.configureConnection(sessionID: "new-thread", connected: true)
        await engine.handle(frame: frame("voice_config_ack", ["mode": "realtime", "provider": "openai", "status": "ok"]))
        try check(engine.state == "off" && engine.diagnostic?.contains("conversation") == true, "thread switch stops voice and rejects old-session frames")
        try check(engine.transcripts.isEmpty && engine.acknowledgedProvider == nil, "thread switch clears old voice captions and provider labels")
        let (refused, deniedAudio, deniedWire) = make(); deniedAudio.allowed = false
        await refused.start(mode: "chained", provider: "configured")
        try check(refused.state == "ended" && deniedWire.frames.isEmpty && deniedAudio.startCount == 0, "denied microphone starts no remote provider session")
        let (privacy, privacyAudio, _) = make(); await active(privacy)
        await privacy.handle(frame: frame("voice_status", ["state": "degraded", "privacy_downgrade": true, "summary": "privacy fixture"]))
        try check(privacy.state == "ended" && privacy.privacyRefused && privacyAudio.shutdownCount > 0, "privacy downgrade is refusal and stops capture")
        let (overflow, _, overflowWire) = make(); await active(overflow)
        overflow.receivePCM(Data(count: 4800 * 12)); await settle()
        try check(overflow.state == "ended" && overflow.error?.contains("cannot keep up") == true && overflowWire.count("audio_chunk") == 0, "bounded ingress fails closed before delayed backlog is sent")
        let (mismatch, mismatchAudio, _) = make(); await mismatch.start(mode: "chained", provider: "configured")
        await mismatch.handle(frame: frame("voice_config_ack", ["mode": "realtime", "status": "ok"]))
        try check(mismatch.state == "ended" && mismatchAudio.startCount == 0, "mismatched configuration ack never starts microphone")
        let (missing, missingAudio, missingWire) = make()
        missing.configureConnection(sessionID: " ", connected: true)
        await missing.start(mode: "realtime", provider: "openai")
        try check(!missing.connected && missingAudio.authorizeCount == 0 && missingWire.frames.isEmpty, "blank session never starts permission request or voice")
        let (sendingFailure, _, failedWire) = make(); failedWire.refuse = true
        await sendingFailure.start(mode: "chained", provider: "configured")
        try check(sendingFailure.state == "ended" && sendingFailure.error != nil, "failed configuration send is visible without starting capture")
        let (boundedPlayback, _, _) = make(); await active(boundedPlayback)
        for _ in 0..<34 { await boundedPlayback.handle(frame: frame("audio_delta", ["data_b64": pcm, "encoding": "pcm16", "sample_rate": 24000])) }
        try check(boundedPlayback.error?.contains("queue filled") == true && !boundedPlayback.assistantSpeaking, "bounded playback queue refuses overflow and clears stale audio")
        print("PASS: \(assertions) native voice PCM/protocol/state fixture assertions; no microphone, playback devices, permissions or real providers")
    }
}
