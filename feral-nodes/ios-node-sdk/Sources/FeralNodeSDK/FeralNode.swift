import Foundation

/// Public class vendor apps instantiate. Wraps the HUP WebSocket +
/// vendor adapter lifecycle. A single FeralNode can host multiple
/// adapters concurrently — the phone is a multi-sensor gateway.
///
/// Inbound brain frames the SDK doesn't terminate itself (i.e.
/// everything except `node_ack` / `hup_action_request`) are surfaced
/// on ``inboundFrames`` so a host SwiftUI app can render them.
public actor FeralNode {
    private let brainURL: URL
    private let apiKey: String
    /// HUP node id this FeralNode advertises in ``node_register``.
    /// Exposed publicly (read-only) so adapters that emit envelopes
    /// keyed by node id (e.g. ``glasses_frame.device_id`` for the
    /// phone-camera-as-glasses fallback) can resolve it without
    /// duplicating the value into every adapter init. ``nonisolated``
    /// + ``let`` means callers can read this synchronously without
    /// going through the actor's executor.
    public nonisolated let nodeId: String
    private var socket: HUPWebSocket?
    private var adapters: [VendorAdapter] = []
    private var connected = false

    /// Continuation that drives the public ``inboundFrames`` stream.
    /// Set when the first observer subscribes; tasks emit into it
    /// from `handleInbound`.
    private var inboundContinuation: AsyncStream<HUPFrame>.Continuation?

    /// AsyncStream of every inbound frame the brain sends. The SDK
    /// internally consumes `node_ack` (heartbeat config) and
    /// `hup_action_request` (adapter dispatch) but ALSO yields them
    /// onto this stream so the host can observe them — duplication
    /// is intentional so a host can trace the wire.
    ///
    /// Multiple subscribers are not supported: the actor stores a
    /// single continuation. Hosts should fan out themselves.
    public var inboundFrames: AsyncStream<HUPFrame> {
        AsyncStream { continuation in
            // Replace any prior continuation; only one consumer at
            // a time. The previous consumer's `for-await` loop
            // simply terminates.
            self.inboundContinuation?.finish()
            self.inboundContinuation = continuation
            continuation.onTermination = { @Sendable _ in
                Task { [weak self] in await self?.clearInboundContinuation() }
            }
        }
    }

    private func clearInboundContinuation() {
        self.inboundContinuation = nil
    }

    public init(brainURL: URL, apiKey: String, nodeID: String) {
        self.brainURL = brainURL
        self.apiKey = apiKey
        self.nodeId = nodeID
    }

    /// Register an adapter. Must be called before ``connect()``.
    public func register(adapter: VendorAdapter) {
        adapters.append(adapter)
    }

    /// Open the WebSocket, send node_register with the union of
    /// every adapter's capability strings, then call attach() on
    /// each adapter so they can wire the vendor SDK callbacks.
    ///
    /// On reconnect (jittered backoff inside `HUPWebSocket`), the
    /// `onReconnect` hook re-sends `node_register` so the brain
    /// rebinds the session without the adapters having to know.
    public func connect() async throws {
        let ws = HUPWebSocket(url: brainURL, apiKey: apiKey)
        self.socket = ws
        try await ws.connect(
            onMessage: { [weak self] frame in
                Task { await self?.handleInbound(frame) }
            },
            onReconnect: { [weak self] in
                // Best-effort re-registration after a reconnect; if
                // it fails the receive loop will drop us back into
                // the backoff loop on the next socket error.
                try? await self?.sendNodeRegister()
            }
        )

        try await sendNodeRegister()
        connected = true

        for adapter in adapters {
            try await adapter.attach(to: self)
        }
    }

    /// Send `node_register` with the current adapter capability
    /// union. Called from `connect()` and from `HUPWebSocket`'s
    /// reconnect hook.
    private func sendNodeRegister() async throws {
        guard let socket else { return }
        let capabilities = adapters.map(\.capability)
        let registerPayload = NodeRegisterPayload(
            nodeId: nodeId,
            capabilities: capabilities
        )
        let encoder = JSONEncoder()
        let data = try encoder.encode(registerPayload)
        guard let dict = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw FeralNodeError.malformedFrame(underlying: NSError(
                domain: "FeralNode", code: -1,
                userInfo: [NSLocalizedDescriptionKey: "node_register serialisation failed"]
            ))
        }
        try await socket.send(HUPFrame(
            type: "node_register",
            payload: FeralNode.encodedPayload(dict)
        ))
    }

    /// Disconnect from the brain, optionally tagging the ``node_bye``
    /// frame with a caller-supplied reason. Defaults to ``"shutdown"``
    /// for SDK consumers that don't distinguish; companion app
    /// callers pass ``"user_disconnect"`` so the brain log reflects
    /// the operator action vs an unexpected SDK teardown.
    public func disconnect(reason: String = "shutdown") async {
        for adapter in adapters { await adapter.detach() }
        await socket?.disconnect(reason: reason)
        connected = false
        inboundContinuation?.finish()
        inboundContinuation = nil
    }

    /// Send a node_bye frame for graceful shutdown.
    public func sendNodeBye(reason: String = "shutdown") async throws {
        guard let socket, connected else { throw FeralNodeError.notConnected }
        try await socket.send(HUPFrame(
            type: "node_bye",
            payload: [
                "reason": .string(reason),
                "restart_in_s": .int(0),
            ]
        ))
    }

    /// Used by adapters to emit a ``device_event`` frame.
    public func emit(eventType: String, data: [String: AnyCodable]) async throws {
        guard let socket, connected else { throw FeralNodeError.notConnected }
        let payload: [String: AnyCodable] = [
            "event_type": .string(eventType),
            "node_id": .string(nodeId),
            "data": .object(data),
            "ts": .double(Date().timeIntervalSince1970),
        ]
        try await socket.send(HUPFrame(type: "device_event", payload: payload))
    }

    /// Ergonomic helper for HUP v1.1 ``video_frame``. Matches
    /// the Python SDK's emit_video_frame signature.
    public func emitVideoFrame(
        jpegBase64: String,
        width: Int,
        height: Int,
        sequence: Int = 0,
        keyframe: Bool = true
    ) async throws {
        try await emit(eventType: "video_frame", data: [
            "codec": .string("jpeg"),
            "width": .int(width),
            "height": .int(height),
            "sequence": .int(sequence),
            "keyframe": .bool(keyframe),
            "data_b64": .string(jpegBase64),
        ])
    }

    /// Ergonomic helper for HUP v1.1 ``audio_frame``.
    public func emitAudioFrame(
        opusBase64: String,
        sampleRate: Int = 24000,
        channels: Int = 1,
        sequence: Int = 0,
        frameMs: Int = 20
    ) async throws {
        try await emit(eventType: "audio_frame", data: [
            "codec": .string("opus"),
            "sample_rate": .int(sampleRate),
            "channels": .int(channels),
            "sequence": .int(sequence),
            "frame_ms": .int(frameMs),
            "data_b64": .string(opusBase64),
        ])
    }

    /// Ergonomic helper for HUP v1.3.0 ``glasses_frame`` (HUP_SPEC §5.4.3).
    ///
    /// Emits a top-level ``glasses_frame`` envelope (not a nested
    /// ``device_event``) so the brain's `/v1/node` handler routes the
    /// payload straight into ``state.glasses_buffer`` for the
    /// orchestrator's vision-context-attach.
    ///
    /// ``deviceId`` is the stable id of the glasses hardware. For the
    /// real W610 path it's the W610 unit id (e.g. ``w610-D344``); for
    /// the phone-camera-as-glasses fallback it's the iPhone node id.
    /// ``source`` is forwarded verbatim and labels the provenance
    /// (``"w610"``, ``"camera_fallback"``, ``"jw_w300"``, ...).
    public func emitGlassesFrame(
        deviceId: String,
        jpegBase64: String,
        width: Int? = nil,
        height: Int? = nil,
        source: String = "glasses",
        sequence: Int? = nil
    ) async throws {
        guard let socket, connected else { throw FeralNodeError.notConnected }
        var payload: [String: AnyCodable] = [
            "device_id": .string(deviceId),
            "timestamp": .double(Date().timeIntervalSince1970),
            "encoding": .string("jpeg"),
            "data_b64": .string(jpegBase64),
            "source": .string(source),
        ]
        if let width = width { payload["width"] = .int(width) }
        if let height = height { payload["height"] = .int(height) }
        if let sequence = sequence { payload["sequence"] = .int(sequence) }
        try await socket.send(HUPFrame(type: "glasses_frame", payload: payload))
    }

    /// Ergonomic helper for HUP v1.3.0 ``device_announce`` (HUP_SPEC §5.4.4).
    ///
    /// Used by the iOS BLE scanner to inform the brain of nearby
    /// peripherals so the hardware mesh can record them as memory
    /// entities. Discovery is observation-only — emit this every time
    /// the peripheral is observed; the brain dedupes by ``deviceId``
    /// and updates ``last_seen`` in place.
    public func sendDeviceAnnounce(
        deviceId: String,
        deviceKind: String = "bluetooth_le",
        name: String = "",
        manufacturer: String = "",
        rssiDbm: Int? = nil,
        advertisedServices: [String] = [],
        metadata: [String: AnyCodable] = [:]
    ) async throws {
        guard let socket, connected else { throw FeralNodeError.notConnected }
        var payload: [String: AnyCodable] = [
            "scanner_node_id": .string(nodeId),
            "device_id": .string(deviceId),
            "device_kind": .string(deviceKind),
            "name": .string(name),
            "manufacturer": .string(manufacturer),
            "last_seen": .double(Date().timeIntervalSince1970),
        ]
        if let rssiDbm = rssiDbm { payload["rssi_dbm"] = .int(rssiDbm) }
        if !advertisedServices.isEmpty {
            payload["advertised_services"] = .array(advertisedServices.map { .string($0) })
        }
        if !metadata.isEmpty {
            payload["metadata"] = .object(metadata)
        }
        try await socket.send(HUPFrame(type: "device_announce", payload: payload))
    }

    // MARK: - HUP v1.3 phone-as-peer envelopes
    //
    // The brain accepts these top-level types on /v1/node from a
    // phone-class node so a SwiftUI host app can drive a chat or
    // voice session without the daemon abstraction. See
    // `ASOS/feral-core/api/server.py` `daemon_session` (`chat_request`,
    // `voice_session_start`, `voice_interrupt`, `audio_chunk`).

    /// Send a chat message as a phone-as-peer node. Brain replies
    /// with a `chat_response` frame on ``inboundFrames``.
    ///
    /// Field semantics match `feral-core/models/protocol.py`
    /// `ChatRequestPayload` exactly. `session_id` is REQUIRED; pass
    /// the same value across the lifetime of one conversation so the
    /// brain orchestrator can thread context. `reply_mode` MUST be
    /// `"final"` (one-shot) or `"stream"` (token deltas). `channel`
    /// MUST be `"chat"` or `"vision_ask"`.
    public func sendChatRequest(
        text: String,
        sessionId: String,
        replyMode: ChatReplyMode = .final,
        channel: ChatChannel = .chat,
        replyTo: String? = nil
    ) async throws {
        guard let socket, connected else { throw FeralNodeError.notConnected }
        var payload: [String: AnyCodable] = [
            "session_id": .string(sessionId),
            "text": .string(text),
            "channel": .string(channel.rawValue),
            "reply_mode": .string(replyMode.rawValue),
        ]
        if let replyTo = replyTo {
            payload["reply_to"] = .string(replyTo)
        }
        try await socket.send(HUPFrame(type: "chat_request", payload: payload))
    }

    /// Begin a voice session. Field semantics match
    /// `feral-core/models/protocol.py` `VoiceSessionStartPayload`:
    /// `stream_id` REQUIRED, `sample_rate` REQUIRED, `channels`
    /// REQUIRED. `voice_mode` is read off the raw payload by the
    /// brain's daemon_session voice path and must be one of
    /// `openai_realtime`, `gemini_live`, `chained` (others log a
    /// warning and coerce to `openai_realtime`).
    public func startVoiceSession(
        streamId: String = UUID().uuidString,
        voiceMode: VoiceMode = .openaiRealtime,
        sampleRate: Int = 24000,
        channels: Int = 1,
        languageHint: String = "en-US",
        mode: VoiceCaptureMode = .vad,
        interruptPolicy: InterruptPolicy = .bargeIn,
        cameraLinked: Bool = false
    ) async throws {
        guard let socket, connected else { throw FeralNodeError.notConnected }
        let payload: [String: AnyCodable] = [
            "stream_id": .string(streamId),
            "sample_rate": .int(sampleRate),
            "channels": .int(channels),
            "language_hint": .string(languageHint),
            "mode": .string(mode.rawValue),
            "interrupt_policy": .string(interruptPolicy.rawValue),
            "camera_linked": .bool(cameraLinked),
            // voice_mode is read off the raw payload by daemon_session
            // (not on the Pydantic model) — must be one of the literal
            // values the router accepts.
            "voice_mode": .string(voiceMode.rawValue),
        ]
        try await socket.send(HUPFrame(type: "voice_session_start", payload: payload))
    }

    /// Interrupt an in-flight voice response (user started speaking
    /// before assistant finished). Brain side cancels the pending
    /// realtime/TTS turn.
    public func interruptVoiceSession(streamId: String? = nil) async throws {
        guard let socket, connected else { throw FeralNodeError.notConnected }
        var payload: [String: AnyCodable] = ["node_id": .string(nodeId)]
        if let streamId = streamId { payload["stream_id"] = .string(streamId) }
        try await socket.send(HUPFrame(type: "voice_interrupt", payload: payload))
    }

    /// Stream a chunk of microphone PCM to the brain's voice router.
    /// Pre-conditions: `startVoiceSession()` was called and
    /// `voice_config_ack` (or equivalent acceptance) arrived.
    /// - Parameters:
    ///   - pcmData: Raw PCM-int16 bytes (mono, sample rate matching
    ///     the rate declared in `startVoiceSession`).
    ///   - chunkIndex: Monotonic index. Default uses an internal
    ///     counter — pass an explicit value to control sequencing.
    ///   - isFinal: `true` to flush the utterance and trigger
    ///     end-of-speech on the brain side.
    public func sendAudioChunk(
        pcmData: Data,
        chunkIndex: Int? = nil,
        isFinal: Bool = false,
        sampleRate: Int = 24000,
        encoding: String = "pcm16"
    ) async throws {
        guard let socket, connected else { throw FeralNodeError.notConnected }
        audioChunkSequence += 1
        let idx = chunkIndex ?? audioChunkSequence
        let payload: [String: AnyCodable] = [
            "node_id": .string(nodeId),
            "data_b64": .string(pcmData.base64EncodedString()),
            "chunk_index": .int(idx),
            "is_final": .bool(isFinal),
            "encoding": .string(encoding),
            "sample_rate": .int(sampleRate),
        ]
        try await socket.send(HUPFrame(type: "audio_chunk", payload: payload))
    }
    private var audioChunkSequence: Int = 0

    /// Reply to an inbound `hup_action_request`. Adapters MUST call
    /// this before returning from `handleAction(...)` — otherwise the
    /// brain's mesh times out the action future after `timeout_ms`.
    /// - Parameters:
    ///   - actionId: From the inbound request's `payload.action_id`.
    ///   - success: `true` if the action succeeded.
    ///   - result: Optional structured result (will be available to
    ///     the brain orchestrator as the action future's resolved
    ///     value).
    ///   - error: Optional error message when `success == false`.
    public func sendActionResponse(
        actionId: String,
        success: Bool,
        result: [String: AnyCodable] = [:],
        error: String? = nil
    ) async throws {
        guard let socket, connected else { throw FeralNodeError.notConnected }
        var payload: [String: AnyCodable] = [
            "action_id": .string(actionId),
            "success": .bool(success),
            "ts": .double(Date().timeIntervalSince1970),
        ]
        if !result.isEmpty { payload["result"] = .object(result) }
        if let error = error { payload["error"] = .string(error) }
        try await socket.send(HUPFrame(type: "hup_action_response", payload: payload))
    }

    // MARK: - Inbound dispatch

    private func handleInbound(_ frame: HUPFrame) async {
        // Surface every inbound frame to host observers. Done first
        // so consumer-side code sees the frame even if SDK
        // termination logic mutates state.
        inboundContinuation?.yield(frame)

        if frame.type == "node_ack" {
            var intervalMs = 10000
            if case .int(let ms) = frame.payload["heartbeat_ms"] {
                intervalMs = ms
            }
            await socket?.startHeartbeat(intervalMs: intervalMs)
            return
        }

        if frame.type == "hup_action_request" {
            let actionName: String? = {
                if case .string(let s) = frame.payload["name"] ?? .null { return s }
                return nil
            }()
            guard let action = actionName else { return }
            for adapter in adapters {
                if await adapter.canHandleAction(named: action) {
                    await adapter.handleAction(frame: frame, node: self)
                    return
                }
            }
            // No adapter took the request — emit a not-handled
            // response so the brain's mesh resolves the future
            // promptly instead of timing out.
            if case .string(let actionId) = frame.payload["action_id"] ?? .null {
                try? await sendActionResponse(
                    actionId: actionId,
                    success: false,
                    error: "no adapter handles action: \(action)"
                )
            }
            return
        }
    }

    private static func encodedPayload(_ dict: [String: Any]) -> [String: AnyCodable] {
        var out: [String: AnyCodable] = [:]
        for (k, v) in dict {
            if let s = v as? String { out[k] = .string(s); continue }
            if let b = v as? Bool { out[k] = .bool(b); continue }
            if let i = v as? Int { out[k] = .int(i); continue }
            if let d = v as? Double { out[k] = .double(d); continue }
            if let a = v as? [Any] {
                out[k] = .array(a.compactMap { AnyCodable.from($0) })
                continue
            }
            if let nested = v as? [String: Any] {
                out[k] = .object(encodedPayload(nested))
                continue
            }
            out[k] = .null
        }
        return out
    }
}

extension AnyCodable {
    static func from(_ value: Any) -> AnyCodable? {
        if let s = value as? String { return .string(s) }
        if let b = value as? Bool { return .bool(b) }
        if let i = value as? Int { return .int(i) }
        if let d = value as? Double { return .double(d) }
        return nil
    }
}
