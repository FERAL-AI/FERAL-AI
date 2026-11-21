import SwiftUI
import AVFoundation
import Foundation
import CoreFoundation

private struct NativeVoiceFailure: LocalizedError { let message: String; var errorDescription: String? { message } }

enum NativeVoicePCM {
    static let rate = 24_000.0
    static let chunkBytes = 4_800 // 100 ms, mono PCM16.
    static func encode(_ samples: [Float]) -> Data {
        var bytes = Data(capacity: samples.count * 2)
        for sample in samples {
            let value = sample.isFinite ? max(-1, min(1, sample)) : 0
            let signed = Int16(value < 0 ? value * 32768 : value * 32767)
            let bits = UInt16(bitPattern: signed)
            bytes.append(UInt8(bits & 255)); bytes.append(UInt8(bits >> 8))
        }
        return bytes
    }
    static func decode(_ data: Data) throws -> [Float] {
        guard data.count % 2 == 0 else { throw NativeVoiceFailure(message: "Received an incomplete PCM16 sample.") }
        let bytes = Array(data)
        return stride(from: 0, to: bytes.count, by: 2).map { Float(Int16(bitPattern: UInt16(bytes[$0]) | UInt16(bytes[$0 + 1]) << 8)) / 32768 }
    }
    static func convert(_ input: AVAudioPCMBuffer, converter: AVAudioConverter) throws -> Data {
        let ratio = converter.outputFormat.sampleRate / input.format.sampleRate
        let capacity = AVAudioFrameCount(ceil(Double(input.frameLength) * ratio) + 256)
        guard capacity <= 96_000, let output = AVAudioPCMBuffer(pcmFormat: converter.outputFormat, frameCapacity: capacity) else { throw NativeVoiceFailure(message: "The microphone buffer is too large to convert.") }
        var supplied = false, error: NSError?
        let status = converter.convert(to: output, error: &error) { _, state in
            if supplied { state.pointee = .noDataNow; return nil }
            supplied = true; state.pointee = .haveData; return input
        }
        guard status != .error, error == nil, let channel = output.floatChannelData?[0] else { throw NativeVoiceFailure(message: "Microphone audio conversion failed.") }
        return encode(Array(UnsafeBufferPointer(start: channel, count: Int(output.frameLength))))
    }
}

@MainActor protocol NativeVoiceAudioIO: AnyObject {
    func authorize() async -> Bool
    func startCapture(onPCM: @escaping (Data) -> Void, onFailure: @escaping (String) -> Void) throws
    func play(_ data: Data, encoding: String, sampleRate: Double, completed: @escaping () -> Void) throws
    func clearPlayback()
    func shutdown()
    func stopCapture()
    func finishCapture() async
}

extension NativeVoiceAudioIO {
    func stopCapture() { shutdown() }
    func finishCapture() async { stopCapture() }
}

private final class NativeVoiceCaptureBridge: @unchecked Sendable {
    private let slots = DispatchSemaphore(value: 4)
    private let lock = NSLock()
    private var reportedOverflow = false
    private var closed = false
    private var pending = 0
    private var drain: CheckedContinuation<Void, Never>?
    func close() { lock.lock(); closed = true; lock.unlock() }
    @MainActor func closeAndDrain() async {
        await withCheckedContinuation { waiter in
            lock.lock(); closed = true
            if pending == 0 { lock.unlock(); waiter.resume() }
            else { drain = waiter; lock.unlock() }
        }
    }
    private func delivered() {
        lock.lock(); pending -= 1
        let completed = pending == 0 ? drain : nil
        if pending == 0 { drain = nil }
        lock.unlock(); completed?.resume()
    }
    func submit(_ body: @escaping @MainActor () -> Void, overflow: @escaping @MainActor () -> Void) {
        lock.lock()
        guard !closed else { lock.unlock(); return }
        if slots.wait(timeout: .now()) == .success {
            pending += 1; lock.unlock()
            Task { @MainActor in body(); self.slots.signal(); self.delivered() }
        } else {
            lock.unlock()
            lock.lock(); let report = !reportedOverflow; reportedOverflow = true; lock.unlock()
            if report { Task { @MainActor in overflow() } }
        }
    }
}

// All device access is lazy and occurs only after the user's Start action.
// Apple requires explicit authorization and NSMicrophoneUsageDescription.
@MainActor private final class NativeAVVoiceIO: NativeVoiceAudioIO {
    private var capture: AVAudioEngine?
    private var output: AVAudioEngine?
    private var player: AVAudioPlayerNode?
    private var compressed: AVAudioPlayer?
    private var compressedWatcher: Task<Void, Never>?
    private var installedTap = false
    private var captureBridge: NativeVoiceCaptureBridge?
    func authorize() async -> Bool {
        guard Bundle.main.object(forInfoDictionaryKey: "NSMicrophoneUsageDescription") is String else { return false }
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return true
        case .notDetermined: return await AVCaptureDevice.requestAccess(for: .audio)
        default: return false
        }
    }
    func startCapture(onPCM: @escaping (Data) -> Void, onFailure: @escaping (String) -> Void) throws {
        guard capture == nil else { return }
        let engine = AVAudioEngine(); capture = engine
        let node = engine.inputNode, format = node.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0, let target = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: NativeVoicePCM.rate, channels: 1, interleaved: false), let converter = AVAudioConverter(from: format, to: target) else { shutdown(); throw NativeVoiceFailure(message: "No compatible microphone input format is available.") }
        let bridge = NativeVoiceCaptureBridge(); captureBridge = bridge
        node.installTap(onBus: 0, bufferSize: 2048, format: format) { buffer, _ in
            do { let data = try NativeVoicePCM.convert(buffer, converter: converter); bridge.submit({ onPCM(data) }, overflow: { onFailure("Microphone delivery fell behind; capture stopped to avoid delayed audio.") }) }
            catch { bridge.submit({ onFailure("Microphone conversion failed. Stop voice and try again.") }, overflow: {}) }
        }
        installedTap = true
        do { engine.prepare(); try engine.start() } catch { shutdown(); throw error }
    }
    func play(_ data: Data, encoding: String, sampleRate: Double, completed: @escaping () -> Void) throws {
        if encoding == "pcm16" {
            let samples = try NativeVoicePCM.decode(data)
            guard !samples.isEmpty, sampleRate == NativeVoicePCM.rate, let format = AVAudioFormat(standardFormatWithSampleRate: sampleRate, channels: 1), let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count)) else { throw NativeVoiceFailure(message: "The response PCM format is unsupported.") }
            buffer.frameLength = buffer.frameCapacity
            samples.withUnsafeBufferPointer { buffer.floatChannelData![0].update(from: $0.baseAddress!, count: samples.count) }
            if output == nil {
                let engine = AVAudioEngine(), node = AVAudioPlayerNode(); engine.attach(node); engine.connect(node, to: engine.mainMixerNode, format: format)
                output = engine; player = node; try engine.start(); node.play()
            }
            player!.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { _ in Task { @MainActor in completed() } }
        } else if ["mp3", "wav"].contains(encoding) {
            let audio = try AVAudioPlayer(data: data)
            guard audio.prepareToPlay(), audio.play() else { throw NativeVoiceFailure(message: "The speech audio could not be played.") }
            compressed = audio
            compressedWatcher?.cancel()
            compressedWatcher = Task {
                while !Task.isCancelled && audio.isPlaying { try? await Task.sleep(nanoseconds: 40_000_000) }
                if !Task.isCancelled { completed() }
            }
        } else { throw NativeVoiceFailure(message: "Unsupported speech response encoding: \(encoding).") }
    }
    func clearPlayback() { player?.stop(); player?.play(); compressedWatcher?.cancel(); compressedWatcher = nil; compressed?.stop(); compressed = nil }
    func stopCapture() {
        captureBridge?.close()
        if installedTap { capture?.inputNode.removeTap(onBus: 0) }; installedTap = false
        capture?.stop(); capture = nil
    }
    func finishCapture() async {
        let bridge = captureBridge
        stopCapture()
        await bridge?.closeAndDrain()
        if captureBridge === bridge { captureBridge = nil }
    }
    func shutdown() {
        stopCapture(); captureBridge = nil
        clearPlayback(); output?.stop(); output = nil; player = nil
    }
}

struct NativeVoiceTranscript: Identifiable {
    let id: String
    var role: String
    var text: String
    var partial: Bool
    var confidence: Double?
    var sequence: Int?
}

@MainActor final class NativeVoiceEngine: ObservableObject {
    @Published private(set) var state = "off"
    @Published private(set) var phase = "idle"
    @Published private(set) var error: String?
    @Published private(set) var diagnostic: String?
    @Published private(set) var acknowledgedProvider: String?
    @Published private(set) var reportedProvider: String?
    @Published private(set) var fallbackProvider: String?
    @Published private(set) var privacyRefused = false
    @Published private(set) var muted = false
    @Published private(set) var assistantSpeaking = false
    @Published private(set) var transcripts: [NativeVoiceTranscript] = []
    @Published private(set) var connected = false
    private let sendFrame: ([String: Any]) async throws -> Void
    private let audio: NativeVoiceAudioIO
    private var sessionID: String?
    private var generation = UUID()
    private var playbackGeneration = UUID()
    private var pcmBuffer = Data()
    private var ingress: [Data] = []
    private var outputQueue: [(Data, String, Double)] = []
    private var queuedOutputBytes = 0
    private var sending: Task<Void, Never>?
    private var acknowledgmentDeadline: Task<Void, Never>?
    private var interruptDeadline: Task<Void, Never>?
    private var chunkIndex = 0
    private var ttsIndex = -1
    private var captureRunning = false
    private var playing = false
    private var requestedMode = ""
    private var requestedProvider = ""
    private var providerDegraded = false
    private var awaitingInterrupt = false
    private var voiceAttemptID: String?
    private var stopDispatchAttempt: String?
    private var stopDispatchGeneration: UUID?
    private var interruptRequestID: String?
    private var managed = false
    private var managedGeneration: String?
    private var managedRevision: Int64?
    private var observedGeneration: String?
    private var observedRevision: Int64?
    private var managedReference: NativeTurnReference?
    private var speechCheckpoint: NativeTurnContextCheckpoint?
    private var speechSuppressed = false
    private var managedOutputClosed = false
    private var finishingCapture = false
    private var beginManagedRequest: (() async -> NativeTurnReference?)?
    private var updateManagedTranscript: ((NativeTurnReference, String) async -> Void)?
    private var managedUnknown: ((NativeTurnReference) async -> Void)?
    private var stopManagedTask: (() async -> Void)?
    var hasManagedRequest: Bool { managed && managedReference != nil }
    var isManaged: Bool { managed }
    var canFinishSpeaking: Bool { managed && captureRunning && !finishingCapture }
    func configureManaged(enabled: Bool, generation: String?, revision: Int64?,
                          beginRequest: @escaping () async -> NativeTurnReference?,
                          transcript: @escaping (NativeTurnReference, String) async -> Void,
                          stopTask: @escaping () async -> Void,
                          unknown: @escaping (NativeTurnReference) async -> Void) {
        if managed != enabled && state != "off" { cleanup(); state = "off"; phase = "idle" }
        managed = enabled
        if let generation, let revision { observedGeneration = generation; observedRevision = revision }
        // An active attempt keeps its captured checkpoint; refreshing READY
        // after its terminal must not silently rebind that attempt.
        if managedReference == nil && ["off", "ended"].contains(state) { managedGeneration = generation; managedRevision = revision }
        beginManagedRequest = beginRequest; updateManagedTranscript = transcript; stopManagedTask = stopTask; managedUnknown = unknown
    }
    /// A fresh verified READY observation can revoke queued media, but never
    /// changes the historical terminal or retries the interrupted request.
    func observeManagedContext(generation: String, revision: Int64) {
        guard managed else { return }
        observedGeneration = generation; observedRevision = revision
        guard !["off", "ended"].contains(state) else { return }
        let changedGeneration = managedGeneration != generation
        let changedSpeech = speechCheckpoint.map { $0.generation != generation || $0.revision != revision } ?? false
        let admitting = captureRunning || ["starting", "configured", "saving_request", "awaiting_utterance"].contains(state)
        guard changedGeneration || changedSpeech || (admitting && managedRevision != revision) else { return }
        if let reference = managedReference {
            taskUncertain(); diagnostic = "Saved context changed. Capture and queued speech stopped; check the task status before continuing."
            let callback = managedUnknown; Task { await callback?(reference) }
        } else { fail("Saved context changed before microphone admission. Start again after reviewing the current conversation.") }
    }
    private var maySend: Bool { connected && sessionID != nil && ["active", "degraded"].contains(state) && (captureRunning || finishingCapture) && !muted && !privacyRefused }
    init(sendFrame: @escaping ([String: Any]) async throws -> Void, audio: NativeVoiceAudioIO? = nil) { self.sendFrame = sendFrame; self.audio = audio ?? NativeAVVoiceIO() }
    func configureConnection(sessionID: String?, connected: Bool) {
        let sessionID = sessionID.flatMap { $0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : $0 }
        let connected = connected && sessionID != nil
        guard self.sessionID != sessionID || self.connected != connected else { return }
        let hadSession = self.sessionID != nil && state != "off"
        cleanup(); self.sessionID = sessionID; self.connected = connected
        state = "off"; phase = "idle"
        transcripts = []; acknowledgedProvider = nil; reportedProvider = nil; fallbackProvider = nil; muted = false; privacyRefused = false; error = nil
        if hadSession { diagnostic = "The conversation or connection changed. Voice stopped; start again when ready." }
    }
    private func frame(_ type: String, _ payload: [String: Any]) -> [String: Any] {
        var bound = payload
        if let voiceAttemptID { bound["voice_attempt_version"] = 1; bound["voice_attempt_id"] = voiceAttemptID }
        if managed {
            bound["managed_chained_voice_version"] = 1
            if let managedGeneration { bound["context_generation"] = managedGeneration }
            if let managedRevision { bound["context_revision"] = managedRevision }
            if let managedReference { bound["request_id"] = managedReference.requestID }
        }
        return ["type": type, "hop": "client", "session_id": sessionID ?? "", "payload": bound]
    }
    func matchesStopDispatch(_ frame: [String: Any]) -> Bool {
        guard NativeChatTurnWire.managedVoiceStop(frame), stopDispatchGeneration == generation,
              let stopDispatchAttempt, frame["session_id"] as? String == sessionID,
              let payload = frame["payload"] as? [String: Any], payload["voice_attempt_id"] as? String == stopDispatchAttempt else { return false }
        return true
    }
    private func matchesAttempt(_ payload: [String: Any]) -> Bool {
        guard let voiceAttemptID, payload["voice_attempt_id"] as? String == voiceAttemptID,
              let version = payload["voice_attempt_version"] as? NSNumber,
              CFGetTypeID(version) != CFBooleanGetTypeID(),
              !["f", "d"].contains(String(cString: version.objCType)), version.int64Value == 1 else { return false }
        return true
    }
    func start(mode: String, provider: String) async {
        guard connected, sessionID != nil, state == "off" || state == "ended", ["realtime", "chained"].contains(mode), !provider.isEmpty else { error = "Connect to a conversation before starting voice."; return }
        if managed { managedGeneration = observedGeneration; managedRevision = observedRevision }
        if managed && (mode != "chained" || provider != "configured" || managedGeneration == nil || managedRevision == nil) {
            error = "Saved-context voice requires the configured chained pipeline and a verified saved context."; return
        }
        cleanup(); error = nil; diagnostic = nil; transcripts = []; acknowledgedProvider = nil; reportedProvider = nil; fallbackProvider = nil; privacyRefused = false; muted = false
        requestedMode = mode; requestedProvider = provider; providerDegraded = false; state = "authorizing"
        voiceAttemptID = UUID().uuidString.lowercased()
        let current = generation
        guard await audio.authorize() else { if current == generation { state = "ended"; error = "Microphone access is unavailable. Check the app's microphone permission and try Start again." }; return }
        guard current == generation, connected else { return }
        state = "starting"
        do {
            try await sendFrame(frame("voice_config", ["mode": mode, "provider": provider, "supports_realtime": mode == "realtime"]))
            guard current == generation, state == "starting" else { return }
            acknowledgmentDeadline = Task { [weak self] in
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                guard !Task.isCancelled, let self, self.generation == current, self.state == "starting" else { return }
                self.fail("The agent did not acknowledge voice configuration. No microphone audio was sent.")
            }
        } catch { if current == generation { fail("Voice configuration could not be sent. Reconnect and try again.") } }
    }
    func stop() async {
        let message = frame("voice_config", ["mode": "disabled"])
        let selectedGeneration = generation, selectedAttempt = voiceAttemptID
        let couldSend = connected && sessionID != nil
        if managedReference != nil {
            audio.stopCapture(); captureRunning = false; finishingCapture = false
            sending?.cancel(); sending = nil; ingress = []; pcmBuffer = Data(); speechSuppressed = true; flushPlayback()
            await stopManagedTask?()
        }
        guard generation == selectedGeneration else { return }
        cleanup(); state = "off"; phase = "idle"
        let current = generation
        if managed { stopDispatchAttempt = selectedAttempt; stopDispatchGeneration = current }
        defer { if generation == current { stopDispatchAttempt = nil; stopDispatchGeneration = nil } }
        if couldSend { do { try await sendFrame(message) } catch { if generation == current { self.error = "Local capture and playback stopped; the remote stop request could not be confirmed." } } }
    }
    private func cleanup() {
        generation = UUID(); acknowledgmentDeadline?.cancel(); acknowledgmentDeadline = nil; interruptDeadline?.cancel(); interruptDeadline = nil; sending?.cancel(); sending = nil
        audio.shutdown(); captureRunning = false; pcmBuffer = Data(); ingress = []; chunkIndex = 0
        managedReference = nil; speechCheckpoint = nil; speechSuppressed = false; managedOutputClosed = false; finishingCapture = false
        awaitingInterrupt = false; voiceAttemptID = nil; interruptRequestID = nil
        stopDispatchAttempt = nil; stopDispatchGeneration = nil
        flushPlayback(); ttsIndex = -1
    }
    private func fail(_ message: String) {
        let uncertain = managed ? managedReference : nil, callback = managedUnknown
        cleanup(); state = "ended"; phase = "error"; error = message
        if let uncertain { Task { await callback?(uncertain) } }
    }
    func setMuted(_ value: Bool) async {
        guard captureRunning, connected else { return }
        muted = value; pcmBuffer = Data(); ingress = []
        let current = generation
        do { try await sendFrame(frame("voice_mute", ["muted": value])) }
        catch { if current == generation { muted = true; fail("The microphone mute change could not be delivered. Local capture stopped.") } }
    }
    func interrupt() async {
        guard connected, captureRunning || (managed && managedReference != nil) else { return }
        speechSuppressed = managed; flushPlayback(); phase = "listening"
        diagnostic = "Queued playback stopped locally. Requesting remote response cancellation…"
        awaitingInterrupt = true
        let requestID = UUID().uuidString.lowercased(); interruptRequestID = requestID
        let current = generation
        do {
            try await sendFrame(frame("voice_interrupt", ["voice_request_id": requestID]))
            guard generation == current, awaitingInterrupt, interruptRequestID == requestID else { return }
            interruptDeadline?.cancel()
            interruptDeadline = Task { [weak self] in
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                guard !Task.isCancelled, let self, self.generation == current, self.interruptRequestID == requestID else { return }
                self.diagnostic = "Queued playback stopped locally; remote cancellation was not acknowledged."
            }
        } catch { if generation == current, interruptRequestID == requestID { diagnostic = "Queued playback stopped locally; the cancellation request could not be sent." } }
    }
    private func flushPlayback() {
        playbackGeneration = UUID(); outputQueue = []; queuedOutputBytes = 0; playing = false; assistantSpeaking = false; audio.clearPlayback()
    }
    func receivePCM(_ data: Data) {
        guard maySend else { return }
        guard data.count <= 192_000, data.count % 2 == 0 else { fail("Microphone data was malformed or too large; capture stopped."); return }
        pcmBuffer.append(data)
        while pcmBuffer.count >= NativeVoicePCM.chunkBytes {
            guard ingress.count < 10 else { fail("The voice connection cannot keep up with the microphone. Capture stopped to avoid sending delayed audio."); return }
            ingress.append(Data(pcmBuffer.prefix(NativeVoicePCM.chunkBytes))); pcmBuffer.removeFirst(NativeVoicePCM.chunkBytes)
        }
        pumpIngress()
    }
    private func pumpIngress() {
        guard sending == nil, !ingress.isEmpty else { return }
        let current = generation
        sending = Task { [weak self] in
            guard let self else { return }
            defer { if self.generation == current { self.sending = nil } }
            while !Task.isCancelled, self.generation == current, self.maySend, !self.ingress.isEmpty {
                let bytes = self.ingress.removeFirst(), index = self.chunkIndex; self.chunkIndex += 1
                do { try await self.sendFrame(self.frame("audio_chunk", ["encoding": "pcm16", "sample_rate": 24000, "channels": 1, "chunk_index": index, "is_final": false, "data_b64": bytes.base64EncodedString()])) }
                catch { if self.generation == current { self.fail("Microphone audio could not be sent. Local capture stopped.") }; return }
            }
        }
    }
    /// One explicit utterance per attempt. A fresh Start is required for another.
    private func beginUtterance() async {
        guard managed, state == "configured", managedReference == nil else { return }
        let current = generation
        state = "saving_request"
        let prepared = await beginManagedRequest?()
        guard current == generation else {
            if let prepared { await managedUnknown?(prepared) }; return
        }
        guard let reference = prepared, reference.sessionID == sessionID, NativeChatTurnWire.uuid(reference.requestID) else {
            fail("The voice request could not be saved and verified. No microphone audio was sent."); return
        }
        managedReference = reference; state = "awaiting_utterance"
        do {
            try await sendFrame(frame("voice_utterance_begin", [:]))
            guard current == generation, state == "awaiting_utterance" else { return }
            acknowledgmentDeadline = Task { [weak self] in
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                guard !Task.isCancelled, let self, self.generation == current, self.state == "awaiting_utterance" else { return }
                self.audio.stopCapture(); self.captureRunning = false; self.state = "uncertain"
                self.error = "Voice admission was not confirmed. Check the saved request status; it was not retried."
                if let reference = self.managedReference { await self.managedUnknown?(reference) }
            }
        } catch {
            if current == generation { state = "uncertain"; self.error = "Voice admission could not be confirmed. No audio was sent; check request status before starting again."; if let reference = managedReference { await managedUnknown?(reference) } }
        }
    }
    func finishSpeaking() async {
        guard canFinishSpeaking else { return }
        let current = generation
        finishingCapture = true
        await audio.finishCapture()
        guard current == generation, finishingCapture else { return }
        captureRunning = false
        // Device capture is stopped and all admitted callback deliveries have
        // drained. Flush their ordered ingress before the explicit finish.
        if !pcmBuffer.isEmpty { ingress.append(pcmBuffer); pcmBuffer = Data() }
        pumpIngress()
        while let pending = sending { await pending.value; guard generation == current else { return } }
        guard generation == current, finishingCapture, state == "active" || state == "degraded" else { return }
        finishingCapture = false; state = "processing"; phase = "processing"
        do { try await sendFrame(frame("voice_utterance_finish", [:])) }
        catch { if generation == current { state = "uncertain"; self.error = "The voice submission is unconfirmed. Check request status; it has not been retried."; if let reference = managedReference { await managedUnknown?(reference) } } }
    }
    func taskUncertain() {
        guard managed, managedReference != nil else { return }
        audio.stopCapture(); captureRunning = false; finishingCapture = false
        sending?.cancel(); sending = nil; ingress = []; pcmBuffer = Data()
        speechSuppressed = true; speechCheckpoint = nil; flushPlayback(); state = "uncertain"; phase = "idle"
    }
    func accepted(_ reference: NativeTurnReference) {
        guard managed, let current = managedReference, reference.sessionID == current.sessionID,
              reference.requestID == current.requestID, let turn = reference.turnID,
              current.turnID == nil || current.turnID == turn else { return }
        managedReference = reference; phase = "processing"
    }
    func terminal(_ terminal: NativeTurnTerminal) {
        guard managed, let reference = managedReference,
              terminal.reference.sessionID == reference.sessionID, terminal.reference.requestID == reference.requestID,
              reference.turnID != nil, terminal.reference.turnID == reference.turnID else { return }
        audio.stopCapture(); captureRunning = false; finishingCapture = false
        guard !terminal.replayed, !speechSuppressed,
              ["completed", "awaiting_approval", "refused"].contains(terminal.outcome),
              let checkpoint = terminal.contextCheckpoint, checkpoint.generation == managedGeneration,
              let expected = managedRevision, expected <= Int64.max - 2, checkpoint.revision == expected + 2,
              observedGeneration == checkpoint.generation,
              observedRevision == expected || observedRevision == checkpoint.revision else {
            speechCheckpoint = nil; speechSuppressed = true; flushPlayback(); state = "completed"; phase = "idle"; return
        }
        speechCheckpoint = checkpoint; managedOutputClosed = false; state = "completed"; phase = "idle"
    }
    private func matchesManagedIdentity(_ payload: [String: Any]) -> Bool {
        managed && NativeChatTurnWire.version(payload["managed_chained_voice_version"]) &&
            managedReference != nil && payload["request_id"] as? String == managedReference?.requestID
    }
    private func refuseUtteranceACK(_ payload: [String: Any]) async {
        guard matchesManagedIdentity(payload), payload["status"] as? String == "error",
              NativeChatTurnWire.boolean(payload["retry_safe"]) == false,
              let code = payload["code"] as? String, !code.isEmpty, code.utf8.count <= 256 else { return }
        acknowledgmentDeadline?.cancel(); acknowledgmentDeadline = nil
        taskUncertain(); error = "The voice request was refused or became unavailable. Check its status; no task was retried and cancellation is not confirmed."
        if let reference = managedReference { await managedUnknown?(reference) }
    }
    private func matchesManagedRequest(_ payload: [String: Any], speech: Bool = false) -> Bool {
        guard managed, NativeChatTurnWire.version(payload["managed_chained_voice_version"]),
              let reference = managedReference, payload["request_id"] as? String == reference.requestID else { return false }
        if speech {
            guard !speechSuppressed, !managedOutputClosed, let checkpoint = speechCheckpoint,
                  payload["turn_id"] as? String == reference.turnID,
                  NativeTurnContextCheckpoint.parse(payload["context_checkpoint"], sessionID: reference.sessionID) == checkpoint else { return false }
        } else {
            guard let checkpoint = NativeTurnContextCheckpoint.parse(payload["context_checkpoint"], sessionID: reference.sessionID),
                  checkpoint.generation == managedGeneration, checkpoint.revision == managedRevision else { return false }
        }
        return true
    }
    private func startMicrophone() {
        let current = generation
        do {
            try audio.startCapture(onPCM: { [weak self] data in guard let self, self.generation == current, self.captureRunning else { return }; self.receivePCM(data) },
                onFailure: { [weak self] message in guard let self, self.generation == current else { return }; self.fail(message) })
            captureRunning = true; state = providerDegraded ? "degraded" : "active"; phase = "listening"
        } catch { fail("The microphone could not start. Check the input device and app permissions.") }
    }
    func handle(frame: [String: Any]) async {
        guard connected, let sessionID, frame["session_id"] as? String == sessionID, !["off", "ended", "authorizing"].contains(state) else { return }
        let type = frame["type"] as? String ?? "", payload = frame["payload"] as? [String: Any] ?? [:]
        // A SID survives Stop/Start. Only the identity sent by this Start may
        // authorize capture or publish media/status; never infer it from arrival.
        guard matchesAttempt(payload) else { return }
        if managed {
            if type == "voice_utterance_begin_ack" {
                if state == "awaiting_utterance", payload["status"] as? String == "error" { await refuseUtteranceACK(payload); return }
                guard state == "awaiting_utterance", matchesManagedRequest(payload), payload["status"] as? String == "collecting" else { return }
                acknowledgmentDeadline?.cancel(); acknowledgmentDeadline = nil; startMicrophone(); return
            }
            if type == "voice_utterance_finish_ack" {
                if state == "processing", payload["status"] as? String == "error" { await refuseUtteranceACK(payload); return }
                guard ["processing", "completed"].contains(state), matchesManagedIdentity(payload),
                      payload["status"] as? String == "submitted_processing",
                      NativeChatTurnWire.boolean(payload["task_accepted"]) == false else { return }
                diagnostic = "Voice submitted; waiting for the tracked task receipt."; return
            }
            if type == "voice_state" {
                guard matchesManagedRequest(payload) || matchesManagedRequest(payload, speech: true) else { return }
                if payload["state"] as? String == "error" {
                    taskUncertain(); error = "The voice pipeline failed. Check the tracked task status; cancellation is not confirmed."
                    if let reference = managedReference { await managedUnknown?(reference) }; return
                }
            }
            if ["transcript", "speech_started", "audio_response", "audio_delta", "tts_chunk", "audio_chunk", "voice_cancel"].contains(type) {
                let userTranscript = type == "transcript" && payload["role"] as? String == "user"
                guard matchesManagedRequest(payload, speech: !userTranscript) else { return }
                if userTranscript, payload["is_partial"] as? Bool != true, let text = payload["text"] as? String, let reference = managedReference {
                    await updateManagedTranscript?(reference, text)
                }
            }
        }
        // Status may precede configuration acknowledgment. Media never may:
        // queued frames from an earlier run must not play while a new run waits.
        if ["voice_state", "transcript", "speech_started", "audio_response", "audio_delta", "tts_chunk", "audio_chunk", "voice_cancel"].contains(type) {
            guard managed ? (managedReference != nil) : (captureRunning && ["active", "degraded"].contains(state)) else { return }
        }
        switch type {
        case "voice_interrupt_ack":
            guard awaitingInterrupt, let interruptRequestID,
                  payload["voice_request_id"] as? String == interruptRequestID else { return }
            awaitingInterrupt = false; self.interruptRequestID = nil
            interruptDeadline?.cancel(); interruptDeadline = nil
            let status = payload["status"] as? String ?? "unknown"
            if payload["cancel_requested"] as? Bool == true && status == "requested" {
                diagnostic = "The agent requested response cancellation. This does not confirm that all remote audio has stopped."
            } else if status == "no_active_response" { diagnostic = "The agent reports no active response to cancel." }
            else { diagnostic = "Local playback stopped; remote cancellation is not confirmed (\(status))." }
        case "voice_config_ack":
            guard state == "starting" else { return }
            guard payload["status"] as? String == (managed ? "configured" : "ok"), payload["mode"] as? String == requestedMode,
                  payload["provider"] as? String == requestedProvider else { fail("The agent refused or mismatched the voice configuration."); return }
            acknowledgedProvider = payload["provider"] as? String
            acknowledgmentDeadline?.cancel(); acknowledgmentDeadline = nil
            if managed {
                guard NativeChatTurnWire.version(payload["managed_chained_voice_version"]),
                      let checkpoint = NativeTurnContextCheckpoint.parse(payload["context_checkpoint"], sessionID: sessionID),
                      checkpoint.generation == managedGeneration, checkpoint.revision == managedRevision else { fail("The saved-context voice configuration did not match."); return }
                state = "configured"; await beginUtterance()
            } else { startMicrophone() }
        case "voice_status":
            reportedProvider = payload["provider"] as? String; fallbackProvider = payload["fallback_provider"] as? String
            privacyRefused = payload["privacy_downgrade"] as? Bool == true
            if payload["muted"] as? Bool == true { muted = true; pcmBuffer = Data(); ingress = [] }
            diagnostic = [payload["summary"], payload["detail"], payload["recommendation"]].compactMap { $0 as? String }.filter { !$0.isEmpty }.joined(separator: " ")
            if privacyRefused || payload["state"] as? String == "unavailable" { fail(privacyRefused ? "Voice was refused because the reported fallback would violate the selected privacy policy." : "The agent reports voice unavailable.") }
            else if payload["state"] as? String == "degraded" { providerDegraded = true; if captureRunning { state = "degraded" } }
            else if payload["state"] as? String == "available", captureRunning { state = "active" }
        case "voice_state":
            if let next = payload["state"] as? String, ["idle", "listening", "processing", "speaking", "error"].contains(next) { phase = next; if next == "error" { error = payload["error"] as? String ?? "The voice pipeline reported an error." } }
        case "transcript":
            guard let text = payload["text"] as? String else { return }
            let role = payload["role"] as? String ?? "assistant", id = payload["item_id"] as? String
            let sequence = payload["seq"] as? Int
            let row = NativeVoiceTranscript(id: id ?? UUID().uuidString, role: role, text: text, partial: payload["is_partial"] as? Bool == true, confidence: payload["confidence"] as? Double, sequence: sequence)
            if let id, let index = transcripts.firstIndex(where: {$0.id == id}) {
                if let seq = sequence, let old = transcripts[index].sequence, seq < old { return }; transcripts[index] = row
            } else if id == nil, let index = transcripts.indices.last, transcripts[index].partial, transcripts[index].role == role { transcripts[index] = row }
            else { transcripts.append(row); if transcripts.count > 100 { transcripts.removeFirst(transcripts.count - 100) } }
        case "speech_started": flushPlayback(); phase = "listening"
        case "voice_cancel":
            guard payload["drop_pending_audio"] as? Bool == true else { return }
            flushPlayback(); phase = "listening"
            diagnostic = "Queued speech stopped locally. The running task has its own cancellation controls."
        case "audio_response", "audio_delta", "tts_chunk", "audio_chunk":
            do {
                let final = payload["is_final"] as? Bool == true
                let sequenced = type == "tts_chunk" || type == "audio_chunk"
                if sequenced {
                    // Chained output closes with an empty sentinel at the last
                    // emitted index; it must not replay that chunk's bytes.
                    let emptyFinal = final && payload["data_b64"] as? String == ""
                    if let index = payload["chunk_index"] as? Int {
                        guard index >= 0, index > ttsIndex || (emptyFinal && index == ttsIndex) else { throw NativeVoiceFailure(message: "A repeated or out-of-order speech chunk was refused.") }; ttsIndex = index
                    } else if type == "audio_chunk" { throw NativeVoiceFailure(message: "A chained speech chunk without ordering was refused.") }
                }
                let encoding = payload["encoding"] as? String ?? (sequenced ? "mp3" : "pcm16")
                if let base64 = payload["data_b64"] as? String, !base64.isEmpty {
                    guard base64.count <= 2_800_000, let bytes = Data(base64Encoded: base64), bytes.count <= 2_000_000 else { throw NativeVoiceFailure(message: "Speech audio exceeded the local playback bound or was invalid.") }
                    guard ["pcm16", "mp3", "wav"].contains(encoding) else { throw NativeVoiceFailure(message: "The response audio format is unsupported.") }
                    let rate = (payload["sample_rate"] as? Double) ?? NativeVoicePCM.rate
                    if encoding == "pcm16" { _ = try NativeVoicePCM.decode(bytes); guard rate == NativeVoicePCM.rate else { throw NativeVoiceFailure(message: "Only 24 kHz response PCM is supported.") } }
                    guard outputQueue.count < 32, queuedOutputBytes + bytes.count <= 4_000_000 else { throw NativeVoiceFailure(message: "Speech playback queue filled. Stop voice and try again.") }
                    outputQueue.append((bytes, encoding, rate)); queuedOutputBytes += bytes.count; assistantSpeaking = true; phase = "speaking"; pumpOutput()
                }
                if final { if managed { managedOutputClosed = true }; if sequenced { ttsIndex = -1 }; if !playing && outputQueue.isEmpty { assistantSpeaking = false; phase = "listening" } }
            } catch { if managed { speechSuppressed = true }; flushPlayback(); self.error = error.localizedDescription }
        default: break
        }
    }
    private func pumpOutput() {
        guard !playing, !outputQueue.isEmpty else { return }
        let next = outputQueue.removeFirst(); queuedOutputBytes -= next.0.count; playing = true
        let current = playbackGeneration
        do { try audio.play(next.0, encoding: next.1, sampleRate: next.2) { [weak self] in
            guard let self, self.playbackGeneration == current else { return }; self.playing = false
            if self.outputQueue.isEmpty { self.assistantSpeaking = false; self.phase = "listening" } else { self.pumpOutput() }
        } } catch { if managed { speechSuppressed = true }; flushPlayback(); self.error = "Speech audio could not be played. Check the output device or response format." }
    }
}

struct NativeVoiceFeatureView: View {
    let baseURL: URL?
    @ObservedObject var engine: NativeVoiceEngine
    @State private var choice = "chained"
    @State private var reviewedStart = false
    private var localAvailable: Bool { baseURL.map { ["127.0.0.1", "::1", "[::1]"].contains($0.host ?? "") && ["http", "https"].contains($0.scheme ?? "") && $0.user == nil && $0.password == nil } ?? false }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label("Voice conversation", systemImage: "waveform").font(.title2.bold())
            Text("Microphone: \(engine.muted ? "muted" : engine.state == "active" || engine.state == "degraded" ? "capturing" : "stopped") · Session: \(engine.state) · Phase: \(engine.phase)")
            Picker("Voice path", selection: $choice) { Text("Configured chained pipeline").tag("chained"); Text("Request OpenAI realtime").tag("openai"); Text("Request Gemini realtime").tag("gemini") }.disabled(engine.state != "off" && engine.state != "ended")
            Text("Chained recognition and speech use the agent's configured providers. Cloud routes may send your microphone audio to external services; configured fallbacks may differ from the request.").font(.caption).foregroundStyle(.secondary)
            if let provider = engine.acknowledgedProvider { Text("Configuration acknowledgment: \(provider). This acknowledgment alone does not prove provider availability.").font(.caption) }
            if let provider = engine.reportedProvider, !provider.isEmpty { Text("Provider status reports: \(provider)\(engine.fallbackProvider.map { $0.isEmpty ? "" : " · fallback: " + $0 } ?? "")").font(.caption) }
            if let error = engine.error { Text(error).foregroundStyle(.orange) }
            if let diagnostic = engine.diagnostic, !diagnostic.isEmpty { Text(diagnostic).font(.caption).foregroundStyle(.secondary) }
            HStack {
                Button("Start voice…") { reviewedStart = true }.disabled(!localAvailable || !engine.connected || (engine.state != "off" && engine.state != "ended"))
                if engine.isManaged { Button("Finish speaking") { Task { await engine.finishSpeaking() } }.disabled(!engine.canFinishSpeaking) }
                Button(engine.isManaged ? "Stop task and voice" : "Stop") { Task { await engine.stop() } }.disabled(engine.state == "off")
                Button(engine.muted ? "Unmute microphone" : "Mute microphone") { Task { await engine.setMuted(!engine.muted) } }.disabled(!["active", "degraded"].contains(engine.state))
                Button("Interrupt response") { Task { await engine.interrupt() } }.disabled(!engine.assistantSpeaking)
            }
            Text("Wear headphones to reduce speaker feedback. Native echo cancellation and real microphone/provider operation have not yet been verified.").font(.caption).foregroundStyle(.secondary)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    ForEach(engine.transcripts) { row in VStack(alignment: .leading, spacing: 4) { Text(row.role.capitalized + (row.partial ? " · partial" : "")).font(.caption.bold()); NativeSelectableText(row.text); if let confidence = row.confidence { Text("Provider confidence: \(confidence.formatted()) (provider-specific scale)").font(.caption).foregroundStyle(.secondary) } }.padding(10).background(Color.secondary.opacity(0.06)) }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
        }.padding(20)
        .alert("Start microphone voice?", isPresented: $reviewedStart) {
            Button("Cancel", role: .cancel) {}
            Button("Start microphone") { Task { await engine.start(mode: choice == "chained" ? "chained" : "realtime", provider: choice == "chained" ? "configured" : choice) } }
        } message: { Text("Voice starts only after you grant microphone permission and the agent acknowledges configuration. The selected path may use cloud recognition or speech, including configured fallbacks. Stop ends local capture and playback.") }
        .onChange(of: baseURL) { _ in reviewedStart = false }
    }
}
