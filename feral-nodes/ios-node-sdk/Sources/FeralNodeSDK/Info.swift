import Foundation

public enum FeralNodeSDKInfo {
    public static let version = "0.3.0"
    /// HUP wire-protocol version this SDK implements. Synced with
    /// `feral-core/models/protocol.py` (1.3.0). The phone-as-peer
    /// envelopes (chat_request, voice_session_start, audio_chunk)
    /// match the brain's strict Pydantic schemas: literal-typed
    /// `reply_mode`, `channel`; required `session_id` on
    /// `voice_session_start`; required `stream_id` + `channels` on
    /// `audio_chunk`. Adds `glasses_frame` (§5.4.3) for smart-glasses
    /// vision streaming under a per-device circular buffer in
    /// `feral-core/perception/glasses_buffer.py`.
    public static let hupVersion = "1.4.0"
}

// MARK: - Schema-correct enums for phone-as-peer envelopes

/// `chat_request.reply_mode` — must match the Literal in
/// `feral-core/models/protocol.py` `ChatRequestPayload`.
public enum ChatReplyMode: String, Sendable {
    /// One-shot reply; brain emits a single `chat_response` frame.
    case final
    /// Streaming reply; brain emits `stream_delta` frames followed by
    /// a final `chat_response`. Use for typing-indicator UX.
    case stream
}

/// `chat_request.channel` — must match the Literal in
/// `feral-core/models/protocol.py` `ChatRequestPayload`.
public enum ChatChannel: String, Sendable {
    /// Standard chat turn.
    case chat
    /// Vision-grounded ask (image / camera context attached upstream).
    case vision_ask
}

/// `voice_session_start` `voice_mode` — read off raw payload by
/// `daemon_session` (`server.py` ~L1564). Unknown values coerce to
/// `openai_realtime` with a warning, so use these literals.
public enum VoiceMode: String, Sendable {
    case openaiRealtime = "openai_realtime"
    case geminiLive = "gemini_live"
    case chained
}

/// `voice_session_start.mode` — capture style.
public enum VoiceCaptureMode: String, Sendable {
    case pushToTalk = "push_to_talk"
    case holdToTalk = "hold_to_talk"
    /// Voice-activity detection. Default for ambient demos.
    case vad
}

/// `voice_session_start.interrupt_policy`.
public enum InterruptPolicy: String, Sendable {
    /// User starting to speak cancels in-flight TTS.
    case bargeIn = "barge_in"
    /// Strict turn-taking; ignore mic until assistant finishes.
    case strictTurn = "strict_turn"
}

public enum FeralNodeError: Error, LocalizedError {
    /// The vendor adapter was compiled into the package but its
    /// vendor-SDK wire-up has not been completed. Thrown deliberately
    /// so a build never silently succeeds with fake data.
    case adapterNotWired(capability: String, reason: String)
    case notConnected
    case brainRejected(code: Int, message: String)
    case malformedFrame(underlying: Error)
    /// The user denied a system permission prompt (camera, microphone,
    /// location, etc.). Never silently retried — the adapter surfaces
    /// this so the host app can either guide the user to Settings or
    /// disable the capability entirely.
    case permissionDenied(capability: String, reason: String)

    public var errorDescription: String? {
        switch self {
        case .adapterNotWired(let capability, let reason):
            return "FeralNodeSDK adapter \(capability) is not wired: \(reason). " +
                   "See feral-nodes/ios-node-sdk/README.md → Vendor adapter status."
        case .notConnected:
            return "FeralNode is not connected to the brain."
        case .brainRejected(let code, let message):
            return "Brain rejected the frame (code \(code)): \(message)."
        case .malformedFrame(let underlying):
            return "Malformed HUP frame: \(underlying.localizedDescription)."
        case .permissionDenied(let capability, let reason):
            return "FeralNodeSDK adapter \(capability) could not start: \(reason). " +
                   "The user must grant this permission in system Settings."
        }
    }
}
