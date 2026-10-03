import SwiftUI
import AVFoundation
import Foundation

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
}

private final class NativeVoiceCaptureBridge: @unchecked Sendable {
    private let slots = DispatchSemaphore(value: 4)
    private let lock = NSLock()
    private var reportedOverflow = false
    func submit(_ body: @escaping @MainActor () -> Void, overflow: @escaping @MainActor () -> Void) {
        if slots.wait(timeout: .now()) == .success {
            Task { @MainActor in body(); self.slots.signal() }
        } else {
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
        let bridge = NativeVoiceCaptureBridge()
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
    func shutdown() {
        if installedTap { capture?.inputNode.removeTap(onBus: 0) }; installedTap = false
        capture?.stop(); capture = nil
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
    private var maySend: Bool { connected && sessionID != nil && ["active", "degraded"].contains(state) && captureRunning && !muted && !privacyRefused }
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
    private func frame(_ type: String, _ payload: [String: Any]) -> [String: Any] { ["type": type, "hop": "client", "session_id": sessionID ?? "", "payload": payload] }
    func start(mode: String, provider: String) async {
        guard connected, sessionID != nil, state == "off" || state == "ended", ["realtime", "chained"].contains(mode), !provider.isEmpty else { error = "Connect to a conversation before starting voice."; return }
        cleanup(); error = nil; diagnostic = nil; transcripts = []; acknowledgedProvider = nil; reportedProvider = nil; fallbackProvider = nil; privacyRefused = false; muted = false
        requestedMode = mode; requestedProvider = provider; providerDegraded = false; state = "authorizing"
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
        let couldSend = connected && sessionID != nil
        cleanup(); state = "off"; phase = "idle"
        let current = generation
        if couldSend { do { try await sendFrame(message) } catch { if generation == current { self.error = "Local capture and playback stopped; the remote stop request could not be confirmed." } } }
    }
    private func cleanup() {
        generation = UUID(); acknowledgmentDeadline?.cancel(); acknowledgmentDeadline = nil; interruptDeadline?.cancel(); interruptDeadline = nil; sending?.cancel(); sending = nil
        audio.shutdown(); captureRunning = false; pcmBuffer = Data(); ingress = []; chunkIndex = 0
        awaitingInterrupt = false
        flushPlayback(); ttsIndex = -1
    }
    private func fail(_ message: String) { cleanup(); state = "ended"; phase = "error"; error = message }
    func setMuted(_ value: Bool) async {
        guard captureRunning, connected else { return }
        muted = value; pcmBuffer = Data(); ingress = []
        let current = generation
        do { try await sendFrame(frame("voice_mute", ["muted": value])) }
        catch { if current == generation { muted = true; fail("The microphone mute change could not be delivered. Local capture stopped.") } }
    }
    func interrupt() async {
        guard connected, captureRunning else { return }
        flushPlayback(); phase = "listening"
        diagnostic = "Queued playback stopped locally. Requesting remote response cancellation…"
        awaitingInterrupt = true
        let current = generation
        do {
            try await sendFrame(frame("voice_interrupt", [:]))
            guard generation == current, awaitingInterrupt else { return }
            interruptDeadline?.cancel()
            interruptDeadline = Task { [weak self] in
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                guard !Task.isCancelled, let self, self.generation == current else { return }
                self.diagnostic = "Queued playback stopped locally; remote cancellation was not acknowledged."
            }
        } catch { if generation == current { diagnostic = "Queued playback stopped locally; the cancellation request could not be sent." } }
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
    func handle(frame: [String: Any]) async {
        guard connected, let sessionID, frame["session_id"] as? String == sessionID, !["off", "ended", "authorizing"].contains(state) else { return }
        let type = frame["type"] as? String ?? "", payload = frame["payload"] as? [String: Any] ?? [:]
        // Status may precede configuration acknowledgment. Media never may:
        // queued frames from an earlier run must not play while a new run waits.
        if ["voice_state", "transcript", "speech_started", "audio_response", "audio_delta", "tts_chunk"].contains(type) {
            guard captureRunning, ["active", "degraded"].contains(state) else { return }
        }
        switch type {
        case "voice_interrupt_ack":
            guard awaitingInterrupt else { return }; awaitingInterrupt = false
            interruptDeadline?.cancel(); interruptDeadline = nil
            let status = payload["status"] as? String ?? "unknown"
            if payload["cancel_requested"] as? Bool == true && status == "requested" {
                diagnostic = "The agent requested response cancellation. This does not confirm that all remote audio has stopped."
            } else if status == "no_active_response" { diagnostic = "The agent reports no active response to cancel." }
            else { diagnostic = "Local playback stopped; remote cancellation is not confirmed (\(status))." }
        case "voice_config_ack":
            guard state == "starting" else { return }
            guard payload["status"] as? String == "ok", payload["mode"] as? String == requestedMode,
                  payload["provider"] as? String == requestedProvider else { fail("The agent refused or mismatched the voice configuration."); return }
            acknowledgedProvider = payload["provider"] as? String
            acknowledgmentDeadline?.cancel(); acknowledgmentDeadline = nil
            let current = generation
            do { try audio.startCapture(onPCM: { [weak self] data in guard let self, self.generation == current else { return }; self.receivePCM(data) }, onFailure: { [weak self] message in guard let self, self.generation == current else { return }; self.fail(message) }); captureRunning = true; state = providerDegraded ? "degraded" : "active"; phase = "listening" }
            catch { fail("The microphone could not start. Check the input device and app permissions.") }
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
        case "audio_response", "audio_delta", "tts_chunk":
            do {
                if type == "tts_chunk", let index = payload["chunk_index"] as? Int {
                    guard index >= 0, index > ttsIndex else { throw NativeVoiceFailure(message: "A repeated or out-of-order speech chunk was refused.") }; ttsIndex = index
                }
                let final = payload["is_final"] as? Bool == true
                let encoding = payload["encoding"] as? String ?? (type == "tts_chunk" ? "mp3" : "pcm16")
                if let base64 = payload["data_b64"] as? String, !base64.isEmpty {
                    guard base64.count <= 2_800_000, let bytes = Data(base64Encoded: base64), bytes.count <= 2_000_000 else { throw NativeVoiceFailure(message: "Speech audio exceeded the local playback bound or was invalid.") }
                    guard ["pcm16", "mp3", "wav"].contains(encoding) else { throw NativeVoiceFailure(message: "The response audio format is unsupported.") }
                    let rate = (payload["sample_rate"] as? Double) ?? NativeVoicePCM.rate
                    if encoding == "pcm16" { _ = try NativeVoicePCM.decode(bytes); guard rate == NativeVoicePCM.rate else { throw NativeVoiceFailure(message: "Only 24 kHz response PCM is supported.") } }
                    guard outputQueue.count < 32, queuedOutputBytes + bytes.count <= 4_000_000 else { throw NativeVoiceFailure(message: "Speech playback queue filled. Stop voice and try again.") }
                    outputQueue.append((bytes, encoding, rate)); queuedOutputBytes += bytes.count; assistantSpeaking = true; phase = "speaking"; pumpOutput()
                }
                if final { if type == "tts_chunk" { ttsIndex = -1 }; if !playing && outputQueue.isEmpty { assistantSpeaking = false; phase = "listening" } }
            } catch { flushPlayback(); self.error = error.localizedDescription }
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
        } } catch { flushPlayback(); self.error = "Speech audio could not be played. Check the output device or response format." }
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
                Button("Stop") { Task { await engine.stop() } }.disabled(engine.state == "off")
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
