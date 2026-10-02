import Foundation
import CoreFoundation

struct NativeRuntimeHealthFailure: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
struct NativeRuntimeOwnership: Equatable {
    let generation: UUID
    let instanceID: String
    let baseURL: URL
    let processIdentity: UUID
}
struct NativeRuntimeProbeResult {
    let statusCode: Int
    let instanceHeader: String?
    let agentReady: Bool
    let readinessReason: String?
    init(statusCode: Int, instanceHeader: String?, agentReady: Bool = true, readinessReason: String? = nil) {
        self.statusCode = statusCode; self.instanceHeader = instanceHeader; self.agentReady = agentReady; self.readinessReason = readinessReason
    }
}
enum NativeRuntimeProbeDiagnostics {
    static func transportCode(_ error: Error) -> String? {
        guard let error = error as? URLError else { return nil }
        switch error.code {
        case .timedOut: return "timeout"
        case .cannotConnectToHost: return "connection_unavailable"
        case .networkConnectionLost: return "connection_lost"
        case .notConnectedToInternet: return "network_unavailable"
        default: return nil
        }
    }
}
enum NativeRuntimeReadinessWire {
    private static func boolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }
    static func agentReadiness(_ body: Any) -> (ready: Bool, reason: String?) {
        let unknown = "The service is reachable, but full agent readiness could not be verified. Security recovery remains available."
        guard let value = body as? [String: Any], boolean(value["service_reachable"]) == true,
              let agent = boolean(value["agent_ready"]), let memory = boolean(value["memory_available"]),
              let bootstrap = boolean(value["bootstrap_required"]), let orchestrator = boolean(value["orchestrator_available"]),
              let status = value["status"] as? String, ["ok", "locked", "degraded"].contains(status) else { return (false, unknown) }
        if agent { return memory && !bootstrap && orchestrator && status == "ok" ? (true, nil) : (false, unknown) }
        if !memory { return (false, "The local service is reachable, but stored memory is unavailable. Open Security to review vault recovery; Chat and agent actions are paused.") }
        if bootstrap { return (false, "The service and memory are available, but full agent startup remains paused. Security recovery is available; unlocking alone does not resume the agent.") }
        return (false, "The local service is reachable, but the agent is unavailable. Security recovery remains available.")
    }
}
enum NativeRuntimeHealthPhase: String { case stopped, starting, ready, limited, unavailable, stopping }
struct NativeRuntimeHealthEvent {
    let ownership: NativeRuntimeOwnership
    let phase: NativeRuntimeHealthPhase
    let reason: String
    let requiresExplicitRestart: Bool
    let reconnectVerifiedSession: Bool
    let availableForActions: Bool
    let serviceReachable: Bool
    let automaticActionReplay = false
    let healthWarning: String?
    init(ownership: NativeRuntimeOwnership, phase: NativeRuntimeHealthPhase, reason: String, requiresExplicitRestart: Bool, reconnectVerifiedSession: Bool, availableForActions: Bool, serviceReachable: Bool = false, healthWarning: String? = nil) {
        self.ownership = ownership; self.phase = phase; self.reason = reason; self.requiresExplicitRestart = requiresExplicitRestart
        self.healthWarning = healthWarning; self.reconnectVerifiedSession = reconnectVerifiedSession; self.availableForActions = availableForActions; self.serviceReachable = serviceReachable
    }
}

// This coordinator neither launches/kills processes nor sends user actions.
// The host supplies exact-process liveness, a bounded passive health probe,
// and serialized process launch/stop integration.
@MainActor final class NativeRuntimeHealthCoordinator {
    typealias Probe = (NativeRuntimeOwnership) async throws -> NativeRuntimeProbeResult
    typealias IsRunning = (NativeRuntimeOwnership) -> Bool
    typealias Pause = (Double) async throws -> Void
    private let probe: Probe
    private let isRunning: IsRunning
    private let now: () -> TimeInterval
    private let pause: Pause
    private let onEvent: (NativeRuntimeHealthEvent) -> Void
    private var probeTask: Task<Bool, Never>?
    private var probeID = UUID()
    private var monitorTask: Task<Void, Never>?
    private var monitorID = UUID()
    private var launched = false
    private var expectedStop = false
    private var waking = false
    private var consecutiveFailures = 0
    private var lastReadinessReason: String?
    private var transportFailures = 0
    private var firstTransportFailureAt: TimeInterval?
    private var recoverableTransportFailure = false
    private let transportGraceSeconds: Double
    private(set) var healthWarning: String?
    private var canMonitor: Bool { [.ready, .limited].contains(phase) || (phase == .unavailable && recoverableTransportFailure) }
    private var canProbe: Bool { phase == .starting || canMonitor }
    private(set) var ownership: NativeRuntimeOwnership?
    private(set) var phase: NativeRuntimeHealthPhase = .stopped
    private(set) var sleeping = false
    private(set) var lastVerifiedAt: TimeInterval?
    private(set) var monitorEnabled = false
    var serviceReachable: Bool { [.ready, .limited].contains(phase) && !sleeping && !waking && !expectedStop }
    var availableForActions: Bool { phase == .ready && !sleeping && !waking && !expectedStop }
    init(probe: @escaping Probe, isRunning: @escaping IsRunning, now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }, pause: @escaping Pause = { seconds in try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000)) }, transportGraceSeconds: Double = 45, onEvent: @escaping (NativeRuntimeHealthEvent) -> Void = { _ in }) {
        self.transportGraceSeconds = transportGraceSeconds.isFinite && (5...120).contains(transportGraceSeconds) ? transportGraceSeconds : 45
        self.probe = probe; self.isRunning = isRunning; self.now = now; self.pause = pause; self.onEvent = onEvent
    }
    @discardableResult func prepare(instanceID: String, baseURL: URL, processIdentity: UUID) throws -> NativeRuntimeOwnership {
        guard phase == .stopped, ownership == nil else { throw NativeRuntimeHealthFailure("Retire the previous owned runtime before preparing a replacement.") }
        guard !instanceID.isEmpty, instanceID.utf8.count <= 256, instanceID.trimmingCharacters(in: .whitespacesAndNewlines) == instanceID,
              !instanceID.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              baseURL.scheme == "http", ["127.0.0.1", "::1", "[::1]"].contains(baseURL.host ?? ""), baseURL.user == nil, baseURL.password == nil,
              baseURL.query == nil, baseURL.fragment == nil, ["", "/"].contains(baseURL.path) else { throw NativeRuntimeHealthFailure("Runtime ownership requires a bounded instance ID and literal loopback origin.") }
        let owner = NativeRuntimeOwnership(generation: UUID(), instanceID: instanceID, baseURL: baseURL, processIdentity: processIdentity)
        ownership = owner; phase = .starting; launched = false; expectedStop = false; sleeping = false; waking = false; consecutiveFailures = 0; lastVerifiedAt = nil; lastReadinessReason = nil; transportFailures = 0; firstTransportFailureAt = nil; recoverableTransportFailure = false; healthWarning = nil
        emit(owner, reason: "Owned runtime prepared; launch and health are not yet verified.")
        return owner
    }
    func didLaunch(_ owner: NativeRuntimeOwnership) {
        guard ownership == owner, phase == .starting, !expectedStop else { return }
        launched = true
    }
    func launchFailed(_ owner: NativeRuntimeOwnership) {
        guard ownership == owner, phase == .starting, !expectedStop else { return }
        unavailable(owner, reason: "The owned runtime could not launch. Restart explicitly.")
    }
    func processExited(_ owner: NativeRuntimeOwnership) {
        guard ownership == owner, !expectedStop, ![.stopped, .stopping].contains(phase), phase != .unavailable || recoverableTransportFailure else { return }
        unavailable(owner, reason: "The owned runtime exited. Earlier actions may have taken effect; restart explicitly without replaying them.")
    }
    @discardableResult func beginStop(_ owner: NativeRuntimeOwnership) -> Bool {
        guard ownership == owner, phase != .stopped, phase != .stopping else { return false }
        expectedStop = true; healthWarning = nil; phase = .stopping; waking = false; monitorEnabled = false; cancelTasks()
        emit(owner, reason: "Stopping the exact owned runtime.")
        return true
    }
    @discardableResult func completeStop(_ owner: NativeRuntimeOwnership) -> Bool {
        guard ownership == owner, phase == .stopping, expectedStop else { return false }
        cancelTasks(); phase = .stopped; ownership = nil; launched = false; sleeping = false; expectedStop = false; lastVerifiedAt = nil; consecutiveFailures = 0; transportFailures = 0; firstTransportFailureAt = nil; recoverableTransportFailure = false; healthWarning = nil
        emit(owner, reason: "Owned runtime stopped. Replacement ownership may now be prepared.")
        return true
    }
    func sleep() {
        guard ownership != nil, ![.stopped, .stopping].contains(phase) else { return }
        sleeping = true; waking = false; cancelTasks()
        if let owner = ownership { emit(owner, reason: "Health monitoring paused for sleep. Actions are unavailable until wake verification.") }
    }
    @discardableResult func wake() async -> Bool {
        guard let owner = ownership, sleeping, canMonitor, !expectedStop else { sleeping = false; return false }
        sleeping = false; waking = true
        emit(owner, reason: "Verifying the same owned runtime after wake before reconnecting sessions.")
        let healthy = await verifyNow()
        guard ownership == owner, canMonitor else { return false }
        if monitorEnabled { startMonitor() }
        return healthy
    }
    func beginMonitoring() {
        guard ownership != nil, canMonitor, !expectedStop else { return }
        monitorEnabled = true
        if !sleeping { startMonitor() }
    }
    func endMonitoring() { monitorEnabled = false; monitorID = UUID(); monitorTask?.cancel(); monitorTask = nil }
    private func startMonitor() {
        guard monitorTask == nil, let owner = ownership, !sleeping, canMonitor else { return }
        let token = UUID(); monitorID = token
        monitorTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled, self.monitorID == token, self.ownership == owner, self.canMonitor, !self.sleeping {
                _ = await self.verifyNow()
                guard !Task.isCancelled, self.monitorID == token, self.canMonitor, !self.sleeping else { break }
                do { try await self.pause(5) } catch { break }
            }
            if self.monitorID == token { self.monitorTask = nil }
        }
    }
    @discardableResult func verifyNow() async -> Bool {
        guard let owner = ownership, launched, !sleeping, !expectedStop, canProbe else { return false }
        if let probeTask { return await probeTask.value }
        let token = UUID(); probeID = token
        let task = Task { [weak self] () -> Bool in
            guard let self else { return false }
            guard self.isRunning(owner) else { self.processExited(owner); return false }
            do {
                let result = try await self.probe(owner)
                guard !Task.isCancelled, self.ownership == owner, self.probeID == token, !self.sleeping, !self.expectedStop, self.canProbe else { return false }
                guard self.isRunning(owner) else { self.processExited(owner); return false }
                // Even a non-200 response from a different instance is refused.
                if let header = result.instanceHeader, header != owner.instanceID {
                    self.unavailable(owner, reason: "The health response belonged to a different runtime. This service was refused."); return false
                }
                guard result.statusCode == 200, result.instanceHeader == owner.instanceID else { self.transientFailure(owner); return false }
                let timestamp = self.now()
                guard timestamp.isFinite, timestamp >= 0, self.lastVerifiedAt.map({ timestamp >= $0 }) ?? true else { self.unavailable(owner, reason: "Runtime verification timing was invalid."); return false }
                let hadWarning = self.healthWarning != nil, recovered = self.recoverableTransportFailure
                self.consecutiveFailures = 0; self.transportFailures = 0; self.firstTransportFailureAt = nil; self.recoverableTransportFailure = false; self.healthWarning = nil; self.lastVerifiedAt = timestamp
                let previous = self.phase, resumed = self.waking
                let next: NativeRuntimeHealthPhase = result.agentReady ? .ready : .limited
                let detail = result.readinessReason ?? "The service is reachable, but full agent readiness is unavailable. Security recovery remains available."
                let changed = hadWarning || previous != next || (!result.agentReady && self.lastReadinessReason != detail)
                self.phase = next; self.waking = false; self.lastReadinessReason = result.agentReady ? nil : detail
                if changed || resumed || recovered {
                    self.emit(owner, reason: result.agentReady ? ((resumed || recovered) ? "The same owned runtime was reverified after a connection interruption. Reconnect the exact session and recover replies without replaying actions." : "Owned runtime and full agent readiness verified.") : detail, reconnect: result.agentReady && (resumed || recovered))
                }
                return true
            } catch {
                guard !Task.isCancelled, self.ownership == owner, self.probeID == token, !self.sleeping, !self.expectedStop else { return false }
                if !self.isRunning(owner) { self.processExited(owner) }
                else if let code = NativeRuntimeProbeDiagnostics.transportCode(error) { self.transportFailure(owner, code: code) }
                else { self.transientFailure(owner) }
                return false
            }
        }
        probeTask = task
        let healthy = await task.value
        if probeID == token { probeTask = nil }
        return healthy
    }
    private func transportFailure(_ owner: NativeRuntimeOwnership, code: String) {
        guard ownership == owner else { return }
        // Startup has no prior identity proof to preserve. Warmup owns its retry.
        guard phase != .starting else { transientFailure(owner); return }
        let timestamp = now()
        guard timestamp.isFinite, timestamp >= 0, lastVerifiedAt.map({ timestamp >= $0 }) ?? false else { unavailable(owner, reason: "Runtime verification timing was invalid."); return }
        if firstTransportFailureAt == nil { firstTransportFailureAt = timestamp }
        guard let began = firstTransportFailureAt, timestamp >= began else { unavailable(owner, reason: "Runtime verification timing was invalid."); return }
        transportFailures += 1
        let changed = healthWarning == nil
        healthWarning = "The local agent is responding slowly (" + code + "). Its owned process is still running; checking continues without replaying requests."
        if transportFailures >= 3 && timestamp - began >= transportGraceSeconds {
            guard !recoverableTransportFailure else { return }
            recoverableTransportFailure = true; phase = .unavailable; waking = false
            emit(owner, reason: "The local agent could not be reverified within the transport grace period. Its owned process is still running; passive checks continue. Earlier effects are unknown and requests are not replayed.")
        } else if changed { emit(owner, reason: healthWarning!) }
    }
    private func transientFailure(_ owner: NativeRuntimeOwnership) {
        guard ownership == owner else { return }
        consecutiveFailures += 1
        if consecutiveFailures >= 3 { unavailable(owner, reason: "The owned runtime failed three consecutive health checks. Actions are unavailable; restart explicitly.") }
    }
    private func unavailable(_ owner: NativeRuntimeOwnership, reason: String) {
        guard ownership == owner, !expectedStop, phase != .unavailable || recoverableTransportFailure else { return }
        recoverableTransportFailure = false; healthWarning = nil; phase = .unavailable; waking = false; monitorEnabled = false; cancelTasks(); emit(owner, reason: reason)
    }
    private func cancelTasks() {
        probeID = UUID(); probeTask?.cancel(); probeTask = nil
        monitorID = UUID(); monitorTask?.cancel(); monitorTask = nil
    }
    private func emit(_ owner: NativeRuntimeOwnership, reason: String, reconnect: Bool = false) {
        onEvent(NativeRuntimeHealthEvent(ownership: owner, phase: phase, reason: reason, requiresExplicitRestart: phase == .unavailable && !recoverableTransportFailure, reconnectVerifiedSession: reconnect, availableForActions: availableForActions, serviceReachable: serviceReachable, healthWarning: healthWarning))
    }
}
