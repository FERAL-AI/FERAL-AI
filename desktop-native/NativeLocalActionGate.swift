import Foundation

struct NativeLocalActionGateFailure: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

/// Admission fencing for retained native HTTP clients, not cancellation of an
/// already dispatched effect. Runtime ownership and final-save authority remain
/// with NativeModel. Unmanaged fixture origins begin open at epoch zero.
final class NativeLocalActionGate {
    static let shared = NativeLocalActionGate()
    private struct OriginState { var epoch: UInt64; var open: Bool; var suspensionRevision: UInt64 = 0; var suspended = false }
    private final class Leases: NSObject { var epochs: [String: UInt64] = [:] }
    struct Lease { fileprivate let origin: String; fileprivate let epoch: UInt64; fileprivate let suspensionRevision: UInt64 }
    private let lock = NSLock()
    private var origins: [String: OriginState] = [:]
    private var exhausted = false
    private let sessions = NSMapTable<URLSession, Leases>.weakToStrongObjects()
    private let maximumOrigins: Int
    private let maximumSessions: Int
    private let maximumSessionOrigins: Int

    init(maximumOrigins: Int = 256, maximumSessions: Int = 2048, maximumSessionOrigins: Int = 16) {
        self.maximumOrigins = maximumOrigins
        self.maximumSessions = maximumSessions
        self.maximumSessionOrigins = maximumSessionOrigins
    }

    private static func origin(_ url: URL) -> String? {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let scheme = parts.scheme?.lowercased(), ["http", "https"].contains(scheme),
              parts.user == nil, parts.password == nil, let rawHost = parts.host?.lowercased() else { return nil }
        let host = rawHost == "[::1]" ? "::1" : rawHost
        guard ["127.0.0.1", "localhost", "::1"].contains(host) else { return nil }
        let port = parts.port ?? (scheme == "https" ? 443 : 80)
        guard (1...65535).contains(port) else { return nil }
        return scheme + "://" + (host == "::1" ? "[::1]" : host) + ":" + String(port)
    }

    func pause(origin: URL) throws { try change(origin, open: false) }
    /// Only the host with a newly verified owned runtime calls this method.
    func activate(origin: URL) throws { try change(origin, open: true) }
    /// Only the exact host suspends/resumes admission after passive proof loss.
    /// Runtime epochs are preserved; receipts spanning a suspension are refused.
    func suspend(origin: URL) throws { try changeSuspension(origin, suspended: true) }
    func resume(origin: URL) throws { try changeSuspension(origin, suspended: false) }
    private func changeSuspension(_ url: URL, suspended: Bool) throws {
        guard let key = Self.origin(url) else { throw NativeLocalActionGateFailure(message: "The local runtime origin is invalid. Admission remains unchanged.") }
        lock.lock(); defer { lock.unlock() }
        guard !exhausted else { throw NativeLocalActionGateFailure(message: "Local admission is closed. Restart the app.") }
        var state = origins[key] ?? OriginState(epoch: 0, open: true)
        guard state.open else { throw NativeLocalActionGateFailure(message: "The current runtime cannot resume admission.") }
        guard origins[key] != nil || origins.count < maximumOrigins else {
            exhausted = true
            throw NativeLocalActionGateFailure(message: "Local admission capacity was reached and is closed. Restart the app.")
        }
        guard state.suspended != suspended else { return }
        guard state.suspensionRevision < UInt64.max else { exhausted = true; throw NativeLocalActionGateFailure(message: "Local admission cannot renew. Restart the app.") }
        state.suspensionRevision += 1; state.suspended = suspended; origins[key] = state
    }
    private func change(_ url: URL, open: Bool) throws {
        guard let key = Self.origin(url) else { throw NativeLocalActionGateFailure(message: "The local runtime origin is invalid. Action admission stays unchanged.") }
        lock.lock(); defer { lock.unlock() }
        guard !exhausted else { throw NativeLocalActionGateFailure(message: "Local action admission is closed. Restart the app.") }
        let old = origins[key] ?? OriginState(epoch: 0, open: true)
        guard origins[key] != nil || origins.count < maximumOrigins else {
            exhausted = true
            throw NativeLocalActionGateFailure(message: "Local action admission capacity was reached and is closed. Restart the app.")
        }
        guard old.epoch < UInt64.max else {
            origins[key] = OriginState(epoch: old.epoch, open: false)
            throw NativeLocalActionGateFailure(message: "Local action admission cannot renew. Restart the app.")
        }
        origins[key] = OriginState(epoch: old.epoch + 1, open: open)
    }

    func capture(session: URLSession, url: URL?) throws -> Lease? {
        guard let url, let key = Self.origin(url) else { return nil }
        lock.lock(); defer { lock.unlock() }
        guard !exhausted else { throw NativeLocalActionGateFailure(message: "Local action admission is closed. No request was admitted.") }
        let state = origins[key] ?? OriginState(epoch: 0, open: true)
        let leases: Leases
        if let existing = sessions.object(forKey: session) { leases = existing }
        else {
            // Enumerate live weak keys: dead sessions do not consume capacity or
            // leave identity tokens that could be reused by another URLSession.
            guard sessions.keyEnumerator().allObjects.count < maximumSessions else { throw NativeLocalActionGateFailure(message: "Too many native HTTP clients are retained. No request was admitted.") }
            leases = Leases(); sessions.setObject(leases, forKey: session)
        }
        if leases.epochs[key] == nil {
            guard leases.epochs.count < maximumSessionOrigins else { throw NativeLocalActionGateFailure(message: "This native HTTP client reached its origin limit. No request was admitted.") }
            leases.epochs[key] = state.epoch
        }
        let lease = Lease(origin: key, epoch: leases.epochs[key]!, suspensionRevision: state.suspensionRevision)
        try requireOpen(lease, state: state)
        return lease
    }
    func validate(_ lease: Lease?) throws {
        guard let lease else { return }
        lock.lock(); defer { lock.unlock() }
        guard !exhausted else { throw NativeLocalActionGateFailure(message: "Local action admission is closed. Earlier dispatched effects may be unknown.") }
        try requireOpen(lease, state: origins[lease.origin] ?? OriginState(epoch: 0, open: true))
    }
    private func requireOpen(_ lease: Lease, state: OriginState) throws {
        guard !state.suspended, state.suspensionRevision == lease.suspensionRevision else {
            throw NativeLocalActionGateFailure(message: "Passive runtime verification was interrupted. New actions are paused; earlier effects may be unknown. No automatic retry is made.")
        }
        guard state.open, state.epoch == lease.epoch else {
            throw NativeLocalActionGateFailure(message: "This local client belongs to a stopped or replaced runtime. Earlier dispatched effects may be unknown; no automatic retry is made.")
        }
    }
}

extension URLSession {
    func feralLocalData(for request: URLRequest) async throws -> (Data, URLResponse) {
        try Task.checkCancellation()
        let lease = try NativeLocalActionGate.shared.capture(session: self, url: request.url)
        let result = try await data(for: request)
        try Task.checkCancellation()
        try NativeLocalActionGate.shared.validate(lease)
        return result
    }
}
