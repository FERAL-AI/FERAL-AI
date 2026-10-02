import Foundation
import CoreFoundation

struct NativeSessionRecoveryFailure: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
struct NativeRecoveryTranscript {
    let sessionID: String
    let rows: [[String: Any]]
    // Backend positions are not timestamps or durable message identities.
}
enum NativeSessionRecoveryWire {
    static func validID(_ value: String) -> Bool {
        !value.isEmpty && value.trimmingCharacters(in: .whitespacesAndNewlines) == value && value.utf8.count <= 256 && !value.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) }
    }
    static func segment(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")) ?? ""
    }
    static func integer(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue.isFinite,
              number.doubleValue >= 0, number.doubleValue <= 9_007_199_254_740_991,
              number.doubleValue.rounded(.towardZero) == number.doubleValue else { return nil }
        return number.intValue
    }
    static func primary(_ body: [String: Any]) throws -> String {
        guard body["error"] == nil, let id = body["session_id"] as? String, validID(id) else { throw NativeSessionRecoveryFailure("The shared primary session could not be verified.") }
        return id
    }
    static func transcript(_ body: [String: Any], owner: String, primary: String?) throws -> NativeRecoveryTranscript {
        guard validID(owner), body["error"] == nil, body["session_id"] as? String == owner,
              let rows = body["messages"] as? [[String: Any]], rows.count <= 500,
              let count = integer(body["count"]), count == rows.count,
              JSONSerialization.isValidJSONObject(body), let encoded = try? JSONSerialization.data(withJSONObject: body), encoded.count <= 1_048_576 else {
            throw NativeSessionRecoveryFailure("Transcript recovery was unavailable or returned a different session. Saved messages were retained.")
        }
        if let primary { guard body["primary_session_id"] as? String == primary, owner == primary else { throw NativeSessionRecoveryFailure("The primary transcript owner changed.") } }
        var last = 0
        for row in rows {
            guard let role = row["role"] as? String, ["user", "assistant"].contains(role),
                  let text = row["text"] as? String, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, text.utf8.count <= 65_536,
                  let position = integer(row["ts_ms"]), position > last else { throw NativeSessionRecoveryFailure("The recovered transcript contained malformed or unordered rows.") }
            last = position
        }
        return NativeRecoveryTranscript(sessionID: owner, rows: rows)
    }
    private static func signature(role: String, text: String) -> String { role + "\u{0}" + text.trimmingCharacters(in: .whitespacesAndNewlines) }
    static func merge(existing: [[String: Any]], transcript: NativeRecoveryTranscript) throws -> [[String: Any]] {
        // Revalidate even manually constructed values. No metadata grants authority.
        let validated = try self.transcript(["session_id": transcript.sessionID, "messages": transcript.rows, "count": transcript.rows.count], owner: transcript.sessionID, primary: nil)
        guard existing.count <= 5_000, JSONSerialization.isValidJSONObject(existing), let data = try? JSONSerialization.data(withJSONObject: existing), data.count <= 8_388_608 else { throw NativeSessionRecoveryFailure("Saved messages exceed the recovery budget; they were not replaced.") }
        var unmatched: [String: Int] = [:]
        for row in existing {
            guard let role = row["role"] as? String, ["user", "assistant"].contains(role), let text = (row["content"] as? String) ?? (row["text"] as? String) else { continue }
            unmatched[signature(role: role, text: text), default: 0] += 1
        }
        var merged = existing
        for row in validated.rows {
            let role = row["role"] as! String, text = row["text"] as! String
            let key = signature(role: role, text: text)
            if let remaining = unmatched[key], remaining > 0 { unmatched[key] = remaining - 1; continue }
            merged.append(["id": UUID().uuidString, "role": role, "content": text,
                           "native_recovery": ["runtime_session_id": validated.sessionID, "position": row["ts_ms"]!, "identity_verified": false]])
        }
        guard merged.count <= 5_000, let output = try? JSONSerialization.data(withJSONObject: merged), output.count <= 8_388_608 else { throw NativeSessionRecoveryFailure("Recovered messages exceed the display budget; saved messages were retained.") }
        return merged
    }
}
private final class NativeRecoveryRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}
@MainActor final class NativeSessionRecoveryModel {
    typealias Transport = (URLRequest) async throws -> (Data, HTTPURLResponse)
    private let preferences: UserDefaults
    private let transport: Transport
    private var baseURL: URL?
    private var generation = UUID()
    private var connectionID: UUID?
    private var primaryReadID = UUID()
    private(set) var primarySessionID: String?
    init(preferences: UserDefaults = .standard, transport: Transport? = nil) {
        self.preferences = preferences
        if let transport { self.transport = transport }
        else {
            let config = URLSessionConfiguration.ephemeral; config.timeoutIntervalForRequest = 15; config.timeoutIntervalForResource = 30
            let session = URLSession(configuration: config, delegate: NativeRecoveryRedirectGuard(), delegateQueue: nil)
            self.transport = { request in
                let (data, response) = try await session.data(for: request)
                guard let http = response as? HTTPURLResponse else { throw NativeSessionRecoveryFailure("The local recovery response was unavailable.") }
                return (data, http)
            }
        }
    }
    func configure(baseURL: URL?, connectionID: UUID?) {
        generation = UUID(); primaryReadID = UUID(); self.baseURL = baseURL; self.connectionID = connectionID; primarySessionID = nil
    }
    private func request(_ path: String) async throws -> [String: Any] {
        guard connectionID != nil, let baseURL, baseURL.scheme == "http", ["127.0.0.1", "::1", "[::1]"].contains(baseURL.host ?? ""), baseURL.user == nil, baseURL.password == nil,
              baseURL.query == nil, baseURL.fragment == nil, var parts = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else { throw NativeSessionRecoveryFailure("Recovery requires the verified local agent connection.") }
        let captured = generation, owner = connectionID
        let components = path.split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false)
        parts.percentEncodedPath = String(components[0]); parts.percentEncodedQuery = components.count == 2 ? String(components[1]) : nil
        guard let url = parts.url else { throw NativeSessionRecoveryFailure("The recovery request was invalid.") }
        var request = URLRequest(url: url); request.httpMethod = "GET"
        let (data, response) = try await transport(request)
        guard captured == generation, owner == connectionID, self.baseURL == baseURL else { throw NativeSessionRecoveryFailure("The local agent changed during recovery. No recovered state was applied.") }
        guard (200..<300).contains(response.statusCode), response.url == url, data.count <= 1_048_576,
              let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], body["error"] == nil else { throw NativeSessionRecoveryFailure("Recovery was unavailable. Saved messages were retained.") }
        return body
    }
    func resolvePrimary() async throws -> String {
        primarySessionID = nil; primaryReadID = UUID(); let read = primaryReadID
        let id = try NativeSessionRecoveryWire.primary(try await request("/api/sessions/primary"))
        guard read == primaryReadID else { throw NativeSessionRecoveryFailure("A newer primary-session lookup replaced this recovery.") }
        primarySessionID = id; return id
    }
    func transcript(runtimeSessionID: String) async throws -> NativeRecoveryTranscript {
        guard let primary = primarySessionID, NativeSessionRecoveryWire.validID(runtimeSessionID) else { throw NativeSessionRecoveryFailure("Verify the primary installation before recovering a session.") }
        let path = runtimeSessionID == primary ? "/api/sessions/primary/transcript?limit=500&since_ms=0" : "/api/sessions/" + NativeSessionRecoveryWire.segment(runtimeSessionID) + "/transcript?limit=500&since_ms=0"
        let body = try await request(path)
        guard primarySessionID == primary else { throw NativeSessionRecoveryFailure("The primary installation changed during recovery.") }
        return try NativeSessionRecoveryWire.transcript(body, owner: runtimeSessionID, primary: runtimeSessionID == primary ? primary : nil)
    }
    private func selectionKey() throws -> String {
        guard connectionID != nil, let primarySessionID else { throw NativeSessionRecoveryFailure("Selection cannot be read or saved without a verified primary installation.") }
        return "feral.native.selectedConversation." + primarySessionID.utf8.map { String(format: "%02x", $0) }.joined()
    }
    func selectedConversationID() throws -> String? {
        let stored = preferences.string(forKey: try selectionKey())
        guard let stored else { return nil }
        return NativeSessionRecoveryWire.validID(stored) ? stored : nil
    }
    func rememberSelection(_ conversationID: String?) throws {
        let key = try selectionKey()
        if let conversationID { guard NativeSessionRecoveryWire.validID(conversationID) else { throw NativeSessionRecoveryFailure("The selected conversation ID was invalid.") }; preferences.set(conversationID, forKey: key) }
        else { preferences.removeObject(forKey: key) }
    }
}
