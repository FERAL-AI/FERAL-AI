/**
 FERAL Brain WebSocket Client — iOS Bridge
 =============================================
 Replaces direct OpenAI/Gemini connections. The iPhone becomes a
 full FERAL edge node: it forwards W300 sensor data, camera frames,
 and audio to the Brain, and receives orchestrated responses back.

 Architecture:
   iPhone ←BLE→ FERAL Glasses (W300)
   iPhone ←WebSocket→ FERAL Brain (Mac/Server)
   Brain handles LLM, memory, skills, perception fusion

 Usage:
   let client = FeralBrainClient(host: "192.168.1.100", port: 9090)
   client.connect(apiKey: "your-node-api-key")
   client.sendSensorData(.heartRate, value: ["bpm": 72])
*/

import Foundation
import CoreLocation
import CommonCrypto

// MARK: - Protocol

protocol FeralBrainDelegate: AnyObject {
    func brainDidConnect(sessionId: String)
    func brainDidDisconnect(reason: String)
    func brainDidReceiveResponse(text: String, sdui: [String: Any]?)
    func brainDidReceiveCommand(type: String, payload: [String: Any])
    func brainDidProposeSkill(manifest: [String: Any], reason: String)
    func brainRequestsConfirmation(action: String, tier: String, completion: @escaping (Bool) -> Void)
    func brainDidReceiveAudio(data: Data, encoding: String, sampleRate: Int, isFinal: Bool)
    func brainDidRequestStopPlayback()
    func brainDidReceiveTranscript(text: String, isFinal: Bool)
}

// MARK: - Connection State

enum BrainConnectionState: String {
    case disconnected
    case connecting
    case registering
    case connected
    case reconnecting
}

// MARK: - Sensor Types (matching W300SensorManager)

enum FeralSensorType: String {
    case heartRate = "heart_rate"
    case spo2 = "spo2"
    case temperature = "temperature"
    case uv = "uv"
    case steps = "steps"
    case gesture = "gesture"
}

// MARK: - Location Manager

class FeralLocationManager: NSObject, CLLocationManagerDelegate {
    private let locationManager = CLLocationManager()
    private weak var brainClient: FeralBrainClient?
    private var sendInterval: TimeInterval = 60.0
    private var lastSent: Date = .distantPast
    
    init(brainClient: FeralBrainClient) {
        self.brainClient = brainClient
        super.init()
        locationManager.delegate = self
        locationManager.desiredAccuracy = kCLLocationAccuracyHundredMeters
        locationManager.distanceFilter = 100
    }
    
    func start() {
        locationManager.requestWhenInUseAuthorization()
        locationManager.startUpdatingLocation()
    }
    
    func stop() {
        locationManager.stopUpdatingLocation()
    }
    
    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let location = locations.last,
              Date().timeIntervalSince(lastSent) >= sendInterval else { return }
        lastSent = Date()
        
        let payload: [String: Any] = [
            "latitude": location.coordinate.latitude,
            "longitude": location.coordinate.longitude,
            "altitude": location.altitude,
            "accuracy": location.horizontalAccuracy,
            "speed": location.speed,
            "timestamp": location.timestamp.timeIntervalSince1970,
        ]
        brainClient?.sendSensorData(type: "location", data: payload)
    }
}

// MARK: - QR Code Pairing

/// Legacy QR JSON shape (pre-2026.5.8). Preserved for backward
/// compatibility — when a user re-scans an old QR that was issued by a
/// brain still emitting `mode=app`, we want to pair successfully and
/// log a deprecation warning. New brains emit `UnifiedPairPayload`
/// (see below) and we prefer that path.
struct PairingInfo: Codable {
    let host: String
    let port: Int
    let apiKey: String
    let nodeName: String
}

/// Unified v1 pair payload — emitted by every brain ≥ 2026.5.8.
/// Schema lives at `.internal/audit-v2026.5.5/A4-pairing-redesign.md` §4
/// and is also documented in `feral-nodes/HUP_SPEC.md`.
struct UnifiedPairPayload: Codable {
    let v: Int
    let mode: String
    let url: String
    let token: String
    let brainId: String
    let expires: Int
    let name: String?

    enum CodingKeys: String, CodingKey {
        case v, mode, url, token
        case brainId = "brain_id"
        case expires, name
    }
}

/// Result of `parsePairingPayload`: a normalized `(brain_url, token,
/// brain_id?, name?)` tuple regardless of which legacy or unified
/// shape was scanned. Callers don't need to care which shape it was.
struct PairingDecoded {
    let brainURL: URL
    let token: String
    let brainId: String?
    let name: String?
    let isLegacy: Bool
}

// MARK: - Client

class FeralBrainClient: NSObject {
    
    weak var delegate: FeralBrainDelegate?
    
    private let host: String
    private let port: Int
    private let nodeId: String
    private var apiKey: String = ""
    var useTLS: Bool = false
    
    private var urlSession: URLSession?
    private var webSocket: URLSessionWebSocketTask?
    private var state: BrainConnectionState = .disconnected
    private var sessionId: String?
    
    private var reconnectAttempts = 0
    private let maxReconnectAttempts = 10
    private var reconnectTimer: Timer?
    
    private var sensorBuffer: [[String: Any]] = []
    private let bufferFlushInterval: TimeInterval = 2.0
    private var flushTimer: Timer?
    
    private var offlineQueue: [[String: Any]] = []
    private let maxQueueSize = 1000
    
    private(set) var locationManager: FeralLocationManager?
    
    private let sendQueue = DispatchQueue(label: "com.feral.brain.send", qos: .userInitiated)
    
    init(host: String = "localhost", port: Int = 9090, nodeId: String? = nil, useTLS: Bool = false) {
        self.host = host
        self.port = port
        self.nodeId = nodeId ?? "feral-iphone-\(UIDevice.current.name.lowercased().replacingOccurrences(of: " ", with: "-"))"
        self.useTLS = useTLS
        super.init()
        self.locationManager = FeralLocationManager(brainClient: self)
    }
    
    // MARK: - Connection
    
    func connect(apiKey: String) {
        self.apiKey = apiKey
        state = .connecting
        
        let config = URLSessionConfiguration.default
        config.waitsForConnectivity = true
        urlSession = URLSession(configuration: config, delegate: self, delegateQueue: nil)
        
        let scheme = useTLS ? "wss" : "ws"
        let urlString = "\(scheme)://\(host):\(port)/v1/node"
        guard let url = URL(string: urlString) else {
            state = .disconnected
            return
        }
        
        var request = URLRequest(url: url)
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 15
        
        webSocket = urlSession?.webSocketTask(with: request)
        webSocket?.resume()
        
        locationManager?.start()
        startReceiving()
    }
    
    func disconnect() {
        reconnectTimer?.invalidate()
        flushTimer?.invalidate()
        locationManager?.stop()
        webSocket?.cancel(with: .normalClosure, reason: nil)
        state = .disconnected
    }
    
    // MARK: - Registration
    
    private func registerAsNode() {
        state = .registering
        
        let registration: [String: Any] = [
            "hop": "node",
            "type": "register",
            "payload": [
                "node_id": nodeId,
                "node_type": "phone",
                "capabilities": [
                    "feral_glasses_bridge",
                    "camera",
                    "microphone",
                    "gps",
                    "accelerometer",
                    "gyroscope",
                    "heart_rate",
                    "spo2",
                    "temperature",
                    "uv",
                    "steps"
                ],
                "platform": "ios",
                "model": UIDevice.current.model,
                "os_version": UIDevice.current.systemVersion,
                "glasses_connected": false
            ]
        ]
        
        sendJSON(registration)
    }
    
    // MARK: - Sensor Data
    
    func sendSensorData(_ sensor: FeralSensorType, value: [String: Any]) {
        let telemetry: [String: Any] = [
            "hop": "node",
            "type": "sensor_telemetry",
            "payload": [
                "node_id": nodeId,
                "sensor": sensor.rawValue,
                "data": value,
                "timestamp": ISO8601DateFormatter().string(from: Date()),
                "source": "feral_glasses"
            ]
        ]
        
        sensorBuffer.append(telemetry)
        
        if sensor == .heartRate || sensor == .spo2 {
            flushSensorBuffer()
        }
    }
    
    func sendBatchSensorData(_ readings: [String: [String: Any]]) {
        let batch: [String: Any] = [
            "hop": "node",
            "type": "sensor_batch",
            "payload": [
                "node_id": nodeId,
                "readings": readings,
                "timestamp": ISO8601DateFormatter().string(from: Date()),
                "source": "feral_glasses"
            ]
        ]
        sendJSON(batch)
    }
    
    // MARK: - Generic Sensor Data (with offline queue)
    
    func sendSensorData(type: String, data: [String: Any]) {
        let message: [String: Any] = [
            "hop": "node",
            "type": "sensor_telemetry",
            "payload": [
                "node_id": nodeId,
                "sensor_type": type,
                "data": data,
                "timestamp": ISO8601DateFormatter().string(from: Date())
            ]
        ]
        
        guard isConnected else {
            if offlineQueue.count < maxQueueSize {
                offlineQueue.append(message)
            }
            return
        }
        
        for queued in offlineQueue {
            sendJSON(queued)
        }
        offlineQueue.removeAll()
        
        sendJSON(message)
    }
    
    // MARK: - QR Code Pairing
    
    func generatePairingQR() -> Data? {
        let info = PairingInfo(host: host, port: port, apiKey: apiKey, nodeName: nodeId)
        guard let jsonData = try? JSONEncoder().encode(info) else { return nil }
        
        let filter = CIFilter(name: "CIQRCodeGenerator")!
        filter.setValue(jsonData, forKey: "inputMessage")
        filter.setValue("M", forKey: "inputCorrectionLevel")
        
        guard let ciImage = filter.outputImage else { return nil }
        let transform = CGAffineTransform(scaleX: 10, y: 10)
        let scaledImage = ciImage.transformed(by: transform)
        
        let context = CIContext()
        guard let cgImage = context.createCGImage(scaledImage, from: scaledImage.extent) else { return nil }
        return UIImage(cgImage: cgImage).pngData()
    }
    
    static func parsePairingQR(_ data: Data) -> PairingInfo? {
        return try? JSONDecoder().decode(PairingInfo.self, from: data)
    }

    /// Decode any supported QR or deep-link payload into a uniform
    /// `PairingDecoded`. Accepts:
    ///
    /// 1. The unified v1 JSON `{v:1, mode, url, token, brain_id, …}`
    ///    emitted by brains ≥ 2026.5.8 (preferred).
    /// 2. Legacy `{host, port, apiKey, nodeName}` (pre-2026.5.8 iOS QR).
    /// 3. Legacy `{host, port, token, name}` (pre-2026.5.8 brain `mode=app`).
    /// 4. URL form `feral://pair?p=<base64url-json-payload>` (deep-link entry).
    /// 5. Plain `https://<brain>/pair?t=<token>` URLs (web QR scanned by
    ///    the iOS camera and routed back into the app).
    ///
    /// Returns `nil` if the payload is unrecognised. Logs a deprecation
    /// warning on legacy shapes so we can sunset them in 2026.7.0.
    static func parsePairingPayload(_ data: Data) -> PairingDecoded? {
        // 1. Unified v1.
        if let unified = try? JSONDecoder().decode(UnifiedPairPayload.self, from: data),
           unified.v == 1, let url = URL(string: unified.url) {
            return PairingDecoded(
                brainURL: url,
                token: unified.token,
                brainId: unified.brainId.isEmpty ? nil : unified.brainId,
                name: unified.name,
                isLegacy: false
            )
        }
        // 2 + 3. Legacy {host, port, apiKey|token, nodeName|name}. Try
        // both key sets without erroring out on the absent one.
        if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let host = json["host"] as? String,
           let port = (json["port"] as? Int) ?? Int(json["port"] as? String ?? "") {
            let token = (json["token"] as? String) ?? (json["apiKey"] as? String) ?? ""
            let name = (json["name"] as? String) ?? (json["nodeName"] as? String)
            if !token.isEmpty,
               let url = URL(string: "http://\(host):\(port)") {
                NSLog("[FERAL] parsePairingPayload: accepted legacy {host,port,*} shape; sunset 2026.7.0")
                return PairingDecoded(
                    brainURL: url,
                    token: token,
                    brainId: nil,
                    name: name,
                    isLegacy: true
                )
            }
        }
        // 4. feral:// URL form.
        if let raw = String(data: data, encoding: .utf8),
           let url = URL(string: raw),
           url.scheme == "feral", url.host == "pair",
           let comps = URLComponents(url: url, resolvingAgainstBaseURL: false),
           let pParam = comps.queryItems?.first(where: { $0.name == "p" })?.value,
           let jsonData = decodeBase64URL(pParam) {
            return parsePairingPayload(jsonData)
        }
        // 5. https://<brain>/pair?t=<token>. Treat as v1 with synthesized fields.
        if let raw = String(data: data, encoding: .utf8),
           let url = URL(string: raw),
           let comps = URLComponents(url: url, resolvingAgainstBaseURL: false),
           let token = comps.queryItems?.first(where: { $0.name == "t" })?.value,
           !token.isEmpty,
           let scheme = url.scheme, (scheme == "https" || scheme == "http"),
           let host = url.host {
            // Reconstruct the brain-base URL (drop /pair path).
            var base = URLComponents()
            base.scheme = scheme
            base.host = host
            base.port = url.port
            if let baseURL = base.url {
                return PairingDecoded(
                    brainURL: baseURL,
                    token: token,
                    brainId: nil,
                    name: nil,
                    isLegacy: false
                )
            }
        }
        return nil
    }

    private static func decodeBase64URL(_ s: String) -> Data? {
        var t = s.replacingOccurrences(of: "-", with: "+")
                 .replacingOccurrences(of: "_", with: "/")
        let pad = (4 - t.count % 4) % 4
        t += String(repeating: "=", count: pad)
        return Data(base64Encoded: t)
    }
    
    // MARK: - Camera Frames //
    
    func sendCameraFrame(base64: String, source: String = "rear") {
        let frame: [String: Any] = [
            "hop": "node",
            "type": "frame",
            "payload": [
                "node_id": nodeId,
                "image_b64": base64,
                "source": source,
                "timestamp": ISO8601DateFormatter().string(from: Date())
            ]
        ]
        sendJSON(frame)
    }
    
    // MARK: - Voice Configuration
    
    func sendVoiceConfig(supportsRealtime: Bool = true, mode: String = "realtime") {
        let config: [String: Any] = [
            "hop": "node",
            "type": "voice_config",
            "payload": [
                "node_id": nodeId,
                "supports_realtime": supportsRealtime,
                "mode": mode,
                "sample_rate": 24000,
                "encoding": "pcm16"
            ]
        ]
        sendJSON(config)
    }
    
    // MARK: - Audio
    
    private var audioChunkCounter = 0
    
    func sendAudioChunk(_ data: Data) {
        let base64 = data.base64EncodedString()
        audioChunkCounter += 1
        sendAudioChunk(base64: base64, chunkIndex: audioChunkCounter, isFinal: false)
    }
    
    func sendAudioChunk(base64: String, chunkIndex: Int, isFinal: Bool = false) {
        let audio: [String: Any] = [
            "hop": "node",
            "type": "audio_chunk",
            "payload": [
                "node_id": nodeId,
                "data_b64": base64,
                "chunk_index": chunkIndex,
                "is_final": isFinal,
                "encoding": "pcm16",
                "sample_rate": 24000
            ]
        ]
        sendJSON(audio)
    }
    
    // MARK: - Text Commands
    
    func sendTextCommand(_ text: String, context: [String: Any]? = nil) {
        var msg: [String: Any] = [
            "hop": "node",
            "type": "text_command",
            "payload": [
                "text": text,
                "node_id": nodeId
            ]
        ]
        if let ctx = context {
            var payload = msg["payload"] as? [String: Any] ?? [:]
            payload["context"] = ctx
            msg["payload"] = payload
        }
        sendJSON(msg)
    }
    
    // MARK: - Glasses Status
    
    func updateGlassesStatus(connected: Bool, batteryLevel: Int? = nil) {
        let status: [String: Any] = [
            "hop": "node",
            "type": "glasses_status",
            "payload": [
                "node_id": nodeId,
                "glasses_connected": connected,
                "battery_level": batteryLevel ?? -1,
                "glasses_model": "FERAL"
            ]
        ]
        sendJSON(status)
    }
    
    // MARK: - Skill Approval
    
    func approveSkill(skillId: String) {
        let msg: [String: Any] = [
            "hop": "node",
            "type": "skill_approval",
            "payload": [
                "skill_id": skillId,
                "approved": true
            ]
        ]
        sendJSON(msg)
    }
    
    func rejectSkill(skillId: String) {
        let msg: [String: Any] = [
            "hop": "node",
            "type": "skill_approval",
            "payload": [
                "skill_id": skillId,
                "approved": false
            ]
        ]
        sendJSON(msg)
    }
    
    // MARK: - Internal
    
    private func startReceiving() {
        webSocket?.receive { [weak self] result in
            guard let self = self else { return }
            
            switch result {
            case .success(let message):
                switch message {
                case .string(let text):
                    self.handleMessage(text)
                case .data(let data):
                    if let text = String(data: data, encoding: .utf8) {
                        self.handleMessage(text)
                    }
                @unknown default:
                    break
                }
                self.startReceiving()
                
            case .failure(let error):
                self.handleDisconnect(reason: error.localizedDescription)
            }
        }
    }
    
    private func handleMessage(_ text: String) {
        guard let data = text.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return
        }
        
        let type = json["type"] as? String ?? ""
        let payload = json["payload"] as? [String: Any] ?? [:]
        
        switch type {
        case "registered":
            state = .connected
            sessionId = json["session_id"] as? String ?? ""
            reconnectAttempts = 0
            startSensorFlushTimer()
            sendVoiceConfig(supportsRealtime: true, mode: "realtime")
            DispatchQueue.main.async {
                self.delegate?.brainDidConnect(sessionId: self.sessionId ?? "")
            }
            
        case "text_response":
            let responseText = payload["text"] as? String ?? ""
            let sdui = payload["sdui"] as? [String: Any]
            DispatchQueue.main.async {
                self.delegate?.brainDidReceiveResponse(text: responseText, sdui: sdui)
            }
            
        case "skill_proposal":
            let manifest = payload["manifest"] as? [String: Any] ?? [:]
            let reason = payload["reason"] as? String ?? ""
            DispatchQueue.main.async {
                self.delegate?.brainDidProposeSkill(manifest: manifest, reason: reason)
            }
            
        case "confirmation_required":
            let action = payload["action"] as? String ?? ""
            let tier = payload["tier"] as? String ?? ""
            DispatchQueue.main.async {
                self.delegate?.brainRequestsConfirmation(action: action, tier: tier) { approved in
                    let response: [String: Any] = [
                        "hop": "node",
                        "type": "confirmation_response",
                        "payload": [
                            "action": action,
                            "approved": approved
                        ]
                    ]
                    self.sendJSON(response)
                }
            }
            
        case "audio_response":
            let audioB64 = payload["data_b64"] as? String ?? ""
            let encoding = payload["encoding"] as? String ?? "pcm16"
            let sampleRate = payload["sample_rate"] as? Int ?? 24000
            let isFinal = payload["is_final"] as? Bool ?? false
            if let audioData = Data(base64Encoded: audioB64) {
                DispatchQueue.main.async {
                    self.delegate?.brainDidReceiveAudio(
                        data: audioData, encoding: encoding,
                        sampleRate: sampleRate, isFinal: isFinal
                    )
                }
            }
            
        case "speech_started":
            DispatchQueue.main.async {
                self.delegate?.brainDidRequestStopPlayback()
            }
            
        case "transcript":
            let text = payload["text"] as? String ?? ""
            let isPartial = payload["is_partial"] as? Bool ?? false
            DispatchQueue.main.async {
                self.delegate?.brainDidReceiveTranscript(text: text, isFinal: !isPartial)
            }
            
        case "execute":
            DispatchQueue.main.async {
                self.delegate?.brainDidReceiveCommand(type: type, payload: payload)
            }
            
        default:
            DispatchQueue.main.async {
                self.delegate?.brainDidReceiveCommand(type: type, payload: payload)
            }
        }
    }
    
    private func handleDisconnect(reason: String) {
        state = .disconnected
        flushTimer?.invalidate()
        DispatchQueue.main.async {
            self.delegate?.brainDidDisconnect(reason: reason)
        }
        attemptReconnect()
    }
    
    private func attemptReconnect() {
        guard reconnectAttempts < maxReconnectAttempts else { return }
        state = .reconnecting
        reconnectAttempts += 1
        
        let delay = min(Double(reconnectAttempts) * 2.0, 30.0)
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self = self, self.state == .reconnecting else { return }
            self.connect(apiKey: self.apiKey)
        }
    }
    
    private func sendJSON(_ dict: [String: Any]) {
        sendQueue.async { [weak self] in
            guard let data = try? JSONSerialization.data(withJSONObject: dict),
                  let text = String(data: data, encoding: .utf8) else { return }
            self?.webSocket?.send(.string(text)) { error in
                if let error = error {
                    print("[FERAL Bridge] Send error: \(error)")
                }
            }
        }
    }
    
    private func startSensorFlushTimer() {
        DispatchQueue.main.async {
            self.flushTimer?.invalidate()
            self.flushTimer = Timer.scheduledTimer(
                withTimeInterval: self.bufferFlushInterval,
                repeats: true
            ) { [weak self] _ in
                self?.flushSensorBuffer()
            }
        }
    }
    
    private func flushSensorBuffer() {
        guard !sensorBuffer.isEmpty else { return }
        let batch = sensorBuffer
        sensorBuffer.removeAll()
        for msg in batch {
            sendJSON(msg)
        }
    }
    
    var isConnected: Bool { state == .connected }
    var connectionState: BrainConnectionState { state }
}

// MARK: - URLSession Delegate (with optional TLS pinning)

extension FeralBrainClient: URLSessionWebSocketDelegate {
    
    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didOpenWithProtocol protocol: String?) {
        registerAsNode()
    }
    
    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        let reasonStr = reason.flatMap { String(data: $0, encoding: .utf8) } ?? "Unknown"
        handleDisconnect(reason: "Closed: \(closeCode) — \(reasonStr)")
    }

    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let serverTrust = challenge.protectionSpace.serverTrust else {
            completionHandler(.performDefaultHandling, nil)
            return
        }

        guard let expectedHash = ProcessInfo.processInfo.environment["FERAL_BRAIN_CERT_HASH"],
              !expectedHash.isEmpty else {
            print("[FERAL TLS] No FERAL_BRAIN_CERT_HASH set — using system CAs (not pinned)")
            completionHandler(.performDefaultHandling, nil)
            return
        }

        guard let serverCert = SecTrustCopyCertificateChain(serverTrust) as? [SecCertificate],
              let leafCert = serverCert.first else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }

        let certData = SecCertificateCopyData(leafCert) as Data
        var hash = [UInt8](repeating: 0, count: 32)
        _ = certData.withUnsafeBytes { bytes in
            CC_SHA256(bytes.baseAddress, CC_LONG(certData.count), &hash)
        }
        let certHash = hash.map { String(format: "%02x", $0) }.joined()

        if certHash.lowercased() == expectedHash.lowercased() {
            completionHandler(.useCredential, URLCredential(trust: serverTrust))
        } else {
            print("[FERAL TLS] Certificate pin mismatch! Expected: \(expectedHash), got: \(certHash)")
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }
}
