import Foundation

@main struct NativeRuntimeHealthFeatureTests {
    static var count = 0
    static func check(_ ok: Bool, _ label: String) { guard ok else { fatalError("FAIL: " + label) }; count += 1 }
    static func refuses(_ label: String, _ operation: () throws -> Void) { do { try operation(); fatalError("Accepted: " + label) } catch { count += 1 } }
    @MainActor static func main() async throws {
        let origin = URL(string: "http://127.0.0.1:9465")!
        var running = true, probes = 0, status = 200, header: String? = "instance-a", clock = 100.0
        var events: [NativeRuntimeHealthEvent] = []
        var hold = false, continuation: CheckedContinuation<Void, Never>?
        let coordinator = NativeRuntimeHealthCoordinator(probe: { owner in
            probes += 1
            let capturedStatus = status, capturedHeader = header
            if hold { hold = false; await withCheckedContinuation { continuation = $0 } }
            return NativeRuntimeProbeResult(statusCode: capturedStatus, instanceHeader: capturedHeader)
        }, isRunning: { _ in running }, now: { clock }, onEvent: { events.append($0) })
        check(!(await coordinator.verifyNow()) && probes == 0, "no unowned health probing")
        refuses("remote ownership") { _ = try coordinator.prepare(instanceID: "instance-a", baseURL: URL(string: "http://example.com")!, processIdentity: UUID()) }
        refuses("credential origin") { _ = try coordinator.prepare(instanceID: "instance-a", baseURL: URL(string: "http://user@127.0.0.1")!, processIdentity: UUID()) }
        refuses("blank instance") { _ = try coordinator.prepare(instanceID: " ", baseURL: origin, processIdentity: UUID()) }
        let first = try coordinator.prepare(instanceID: "instance-a", baseURL: origin, processIdentity: UUID())
        check(coordinator.phase == .starting && coordinator.ownership == first && !coordinator.availableForActions, "ownership registered before launch callback")
        check(!(await coordinator.verifyNow()) && probes == 0, "no health request before launch acknowledged")
        refuses("replacement cannot bypass retirement") { _ = try coordinator.prepare(instanceID: "replacement", baseURL: origin, processIdentity: UUID()) }
        coordinator.didLaunch(first)
        check(await coordinator.verifyNow(), "exact health instance establishes readiness")
        check(coordinator.phase == .ready && coordinator.lastVerifiedAt == 100 && coordinator.availableForActions, "verified timestamp and readiness recorded")
        status = 503
        check(!(await coordinator.verifyNow()) && coordinator.phase == .ready, "first transient failure does not declare process death")
        check(!(await coordinator.verifyNow()) && coordinator.phase == .ready, "second transient failure below threshold")
        status = 200; clock = 101
        check(await coordinator.verifyNow(), "successful check resets transient failure sequence")
        status = 503
        _ = await coordinator.verifyNow(); _ = await coordinator.verifyNow()
        check(coordinator.phase == .ready, "failure count reset by successful verification")
        _ = await coordinator.verifyNow()
        check(coordinator.phase == .unavailable && !coordinator.availableForActions && events.last?.requiresExplicitRestart == true, "three consecutive failures require explicit restart")
        let oldEventCount = events.count
        coordinator.processExited(first)
        check(events.count == oldEventCount, "duplicate exit after unavailability does not duplicate crash notification")
        check(coordinator.beginStop(first) && coordinator.phase == .stopping, "exact retirement begins")
        coordinator.processExited(first)
        check(coordinator.phase == .stopping && events.last?.requiresExplicitRestart == false, "expected stop exit suppressed")
        check(coordinator.completeStop(first) && coordinator.ownership == nil, "exact retirement releases ownership")
        status = 200; header = "instance-b"
        let second = try coordinator.prepare(instanceID: "instance-b", baseURL: origin, processIdentity: UUID())
        coordinator.didLaunch(second); _ = await coordinator.verifyNow()
        coordinator.processExited(first)
        check(coordinator.phase == .ready && !coordinator.completeStop(first), "stale process exit and stop cannot affect replacement")
        header = "foreign-runtime"
        _ = await coordinator.verifyNow()
        check(coordinator.phase == .unavailable && events.last?.reason.contains("different runtime") == true, "wrong instance refused immediately")
        _ = coordinator.beginStop(second); _ = coordinator.completeStop(second)
        header = "instance-c"
        let third = try coordinator.prepare(instanceID: "instance-c", baseURL: origin, processIdentity: UUID())
        coordinator.didLaunch(third); _ = await coordinator.verifyNow()
        running = false
        let beforeExitProbe = probes
        check(!(await coordinator.verifyNow()) && probes == beforeExitProbe && coordinator.phase == .unavailable, "definite owned process exit requires no HTTP inference")
        _ = coordinator.beginStop(third); _ = coordinator.completeStop(third)
        running = true; header = "instance-d"
        let fourth = try coordinator.prepare(instanceID: "instance-d", baseURL: origin, processIdentity: UUID())
        coordinator.didLaunch(fourth); _ = await coordinator.verifyNow()
        hold = true
        let pending = Task { await coordinator.verifyNow() }
        while continuation == nil { await Task.yield() }
        let heldCount = probes
        let sharing = Task { await coordinator.verifyNow() }
        for _ in 0..<100 { await Task.yield() }
        check(probes == heldCount, "concurrent checks share one in-flight probe")
        continuation?.resume(); continuation = nil
        let pendingResult = await pending.value, sharingResult = await sharing.value
        check(pendingResult && sharingResult, "shared healthy probe resolves both callers")
        coordinator.sleep()
        let sleepingProbeCount = probes
        let sleepingCheck = await coordinator.verifyNow()
        check(!coordinator.availableForActions && !sleepingCheck && probes == sleepingProbeCount, "sleep pauses probes and action authority")
        check(await coordinator.wake(), "wake verifies same owned runtime")
        check(events.last?.reconnectVerifiedSession == true && events.last?.availableForActions == true, "verified wake requests exact-session reconnect only")
        check(events.allSatisfy { !$0.automaticActionReplay }, "no lifecycle event requests automatic action replay")
        hold = true
        let stale = Task { await coordinator.verifyNow() }
        while continuation == nil { await Task.yield() }
        _ = coordinator.beginStop(fourth); _ = coordinator.completeStop(fourth)
        header = "instance-e"
        let fifth = try coordinator.prepare(instanceID: "instance-e", baseURL: origin, processIdentity: UUID())
        coordinator.didLaunch(fifth); _ = await coordinator.verifyNow()
        continuation?.resume(); continuation = nil
        check(!(await stale.value) && coordinator.ownership == fifth && coordinator.phase == .ready, "cancelled old probe cannot restore or erase replacement authority")
        coordinator.sleep(); header = nil
        check(!(await coordinator.wake()) && !coordinator.availableForActions && coordinator.phase == .ready, "missing wake instance proof cannot authorize actions")
        header = "instance-e"
        check(await coordinator.verifyNow(), "later exact wake proof succeeds")
        check(coordinator.availableForActions && events.last?.reconnectVerifiedSession == true, "wake recovery waits for real proof before session reconnect")
        hold = true
        let diesDuringProbe = Task { await coordinator.verifyNow() }
        while continuation == nil { await Task.yield() }
        running = false; continuation?.resume(); continuation = nil
        check(!(await diesDuringProbe.value) && coordinator.phase == .unavailable, "healthy HTTP cannot authorize a process that exited while probe awaited")
        running = true
        _ = coordinator.beginStop(fifth); _ = coordinator.completeStop(fifth)
        let failed = try coordinator.prepare(instanceID: "launch-failed", baseURL: origin, processIdentity: UUID())
        coordinator.launchFailed(failed)
        check(coordinator.phase == .unavailable && events.last?.requiresExplicitRestart == true, "launch failure retains owned retirement requirement")
        _ = coordinator.beginStop(failed); _ = coordinator.completeStop(failed)
        var pauseCalls = 0, monitorContinuation: CheckedContinuation<Void, Error>?
        let monitor = NativeRuntimeHealthCoordinator(probe: { _ in NativeRuntimeProbeResult(statusCode: 200, instanceHeader: "monitored") }, isRunning: { _ in true }, pause: { _ in
            pauseCalls += 1; try await withCheckedThrowingContinuation { monitorContinuation = $0 }
        })
        let monitored = try monitor.prepare(instanceID: "monitored", baseURL: origin, processIdentity: UUID())
        monitor.didLaunch(monitored); _ = await monitor.verifyNow()
        monitor.beginMonitoring(); monitor.beginMonitoring()
        while monitorContinuation == nil { await Task.yield() }
        check(pauseCalls == 1 && monitor.monitorEnabled, "one monitor loop despite repeated begin calls")
        monitor.sleep(); monitorContinuation?.resume(throwing: CancellationError()); monitorContinuation = nil
        check(monitor.sleeping && monitor.monitorEnabled && !monitor.availableForActions, "sleep cancels monitor while remembering monitoring preference")
        _ = monitor.beginStop(monitored); _ = monitor.completeStop(monitored)
        let readyBody: [String: Any] = ["service_reachable": true, "agent_ready": true, "memory_available": true, "bootstrap_required": false, "orchestrator_available": true, "status": "ok"]
        check(NativeRuntimeReadinessWire.agentReadiness(readyBody).ready, "strict full-agent health body accepted")
        var missingMemory = readyBody; missingMemory["memory_available"] = false
        check(!NativeRuntimeReadinessWire.agentReadiness(missingMemory).ready, "HTTP identity cannot establish agent readiness with missing memory")
        var numericTruth = readyBody; numericTruth["agent_ready"] = 1
        check(!NativeRuntimeReadinessWire.agentReadiness(numericTruth).ready, "numeric truth never substitutes for strict readiness boolean")
        check(!NativeRuntimeReadinessWire.agentReadiness(["status": "ok"]).ready, "legacy unchecked status is reachable but never full-agent proof")
        var limitedEvents: [NativeRuntimeHealthEvent] = [], fullAgent = false
        let limited = NativeRuntimeHealthCoordinator(probe: { _ in NativeRuntimeProbeResult(statusCode: 200, instanceHeader: "limited", agentReady: fullAgent, readinessReason: "Memory locked") }, isRunning: { _ in true }, onEvent: { limitedEvents.append($0) })
        let limitedOwner = try limited.prepare(instanceID: "limited", baseURL: origin, processIdentity: UUID())
        limited.didLaunch(limitedOwner)
        check(await limited.verifyNow(), "locked reachable service is healthy transport")
        check(limited.phase == .limited && limited.serviceReachable && !limited.availableForActions, "limited service keeps Security reachable while agent actions are unavailable")
        check(limitedEvents.last?.serviceReachable == true && limitedEvents.last?.availableForActions == false && limitedEvents.last?.requiresExplicitRestart == false, "limited receipt does not claim full readiness or require process restart")
        let limitedEventCount = limitedEvents.count; _ = await limited.verifyNow()
        check(limitedEvents.count == limitedEventCount, "unchanged limited health does not repeatedly invalidate models")
        limited.sleep(); check(!limited.serviceReachable, "sleep removes even limited endpoint authority")
        check(await limited.wake(), "limited service reverified after wake")
        check(limited.serviceReachable && !limited.availableForActions && limitedEvents.last?.reconnectVerifiedSession == false, "limited wake never reconnects Chat as if agent were ready")
        fullAgent = true; _ = await limited.verifyNow()
        check(limited.phase == .ready && limited.availableForActions && limitedEvents.last?.serviceReachable == true, "agent ready transition requires a new exact probe result")
        _ = limited.beginStop(limitedOwner); _ = limited.completeStop(limitedOwner)
        // Timeouts are lack of timely transport proof, not evidence of process
        // death. Under CPU pressure one delayed callback is one observation.
        var slowClock = 1000.0, slowRunning = true, transportError: URLError?, slowHeader = "slow-owned"
        var slowEvents: [NativeRuntimeHealthEvent] = []
        let slow = NativeRuntimeHealthCoordinator(probe: { _ in
            if let transportError { throw transportError }
            return NativeRuntimeProbeResult(statusCode: 200, instanceHeader: slowHeader)
        }, isRunning: { _ in slowRunning }, now: { slowClock }, transportGraceSeconds: 45, onEvent: { slowEvents.append($0) })
        let slowOwner = try slow.prepare(instanceID: "slow-owned", baseURL: origin, processIdentity: UUID())
        slow.didLaunch(slowOwner); _ = await slow.verifyNow()
        transportError = URLError(.timedOut, userInfo: [NSLocalizedDescriptionKey: "private host/path detail"])
        slowClock = 1200  // An actor scheduling gap is not 200 failed probes.
        _ = await slow.verifyNow()
        slowClock = 1205; _ = await slow.verifyNow()
        slowClock = 1210; _ = await slow.verifyNow()
        check(slow.phase == .ready && slow.availableForActions && slow.serviceReachable, "three short transport misses while exact process lives preserve verified stream authority within bounded grace")
        check(slow.healthWarning?.contains("responding slowly") == true && slowEvents.last?.healthWarning != nil && slowEvents.last?.availableForActions == true, "slow transport is a visible warning distinct from terminal death")
        check(!String(describing: slowEvents.last?.healthWarning).contains("private host/path"), "transport warning contains fixed code not private error contents")
        transportError = nil; slowClock = 1211; _ = await slow.verifyNow()
        check(slow.phase == .ready && slow.healthWarning == nil && slowEvents.last?.reconnectVerifiedSession == false, "same-owner successful proof clears latency warning without replacing or reconnecting a healthy stream")
        transportError = URLError(.cannotConnectToHost)
        slowClock = 1220; _ = await slow.verifyNow()
        slowClock = 1240; _ = await slow.verifyNow()
        slowClock = 1265; _ = await slow.verifyNow()
        check(slow.phase == .unavailable && !slow.availableForActions && slowEvents.last?.requiresExplicitRestart == false, "sustained three transport misses beyond45seconds gate actions but do not claim owned process death")
        check(slow.ownership == slowOwner && slowEvents.last?.automaticActionReplay == false, "recoverable transport retains exact ownership without action replay")
        transportError = nil; slowClock = 1266; _ = await slow.verifyNow()
        check(slow.phase == .ready && slow.ownership == slowOwner && slowEvents.last?.reconnectVerifiedSession == true && slowEvents.last?.healthWarning == nil, "exact health recovers same owned runtime after transport outage without launching replacement or replaying requests")
        transportError = URLError(.timedOut)
        slowClock = 1270; _ = await slow.verifyNow()
        slowClock = 1280; _ = await slow.verifyNow(); slowClock = 1315; _ = await slow.verifyNow()
        check(slow.phase == .unavailable && slowEvents.last?.requiresExplicitRestart == false, "transport outage remains passively recoverable before definite exit")
        slowRunning = false
        _ = await slow.verifyNow()
        check(slow.phase == .unavailable && slowEvents.last?.requiresExplicitRestart == true && slowEvents.last?.reason.contains("exited") == true, "definite process exit during grace remains immediate terminal failure")
        _ = slow.beginStop(slowOwner); _ = slow.completeStop(slowOwner)
        slowRunning = true; transportError = nil; slowHeader = "other-owned"; slowClock = 1300
        let otherOwner = try slow.prepare(instanceID: "other-owned", baseURL: origin, processIdentity: UUID())
        slow.didLaunch(otherOwner); _ = await slow.verifyNow()
        transportError = URLError(.timedOut); slowClock = 1310; _ = await slow.verifyNow()
        slowClock = 1330; _ = await slow.verifyNow(); slowClock = 1355; _ = await slow.verifyNow()
        transportError = nil; slowHeader = "foreign-runtime"; _ = await slow.verifyNow()
        check(slow.phase == .unavailable && slowEvents.last?.requiresExplicitRestart == true && slowEvents.last?.reason.contains("different runtime") == true, "foreign instance during passive transport recovery is refused immediately and never auto-recovers")
        let beforeRefusedProbe = slowEvents.count
        slowHeader = "other-owned"; _ = await slow.verifyNow()
        check(slowEvents.count == beforeRefusedProbe && !slow.availableForActions, "terminal mismatched ownership cannot be rehabilitated by later healthy response")
        _ = slow.beginStop(otherOwner); _ = slow.completeStop(otherOwner)
        check(NativeRuntimeProbeDiagnostics.transportCode(URLError(.timedOut)) == "timeout" && NativeRuntimeProbeDiagnostics.transportCode(URLError(.cancelled)) == nil, "only known transient transport errors receive latency grace; cancellation/protocol failures do not")
        check(slowEvents.allSatisfy { !$0.automaticActionReplay }, "all latency and recovery events forbid automatic action replay")
        var recoveryClock = 2000.0, recoveryError: URLError?, recoveryPause: CheckedContinuation<Void, Error>?
        var recoveryEvents: [NativeRuntimeHealthEvent] = []
        let recoveryMonitor = NativeRuntimeHealthCoordinator(probe: { _ in
            if let recoveryError { throw recoveryError }
            return NativeRuntimeProbeResult(statusCode: 200, instanceHeader: "recovery-monitor")
        }, isRunning: { _ in true }, now: { recoveryClock }, pause: { _ in
            try await withCheckedThrowingContinuation { recoveryPause = $0 }
        }, transportGraceSeconds: 45, onEvent: { recoveryEvents.append($0) })
        let recoveryOwner = try recoveryMonitor.prepare(instanceID: "recovery-monitor", baseURL: origin, processIdentity: UUID())
        recoveryMonitor.didLaunch(recoveryOwner); _ = await recoveryMonitor.verifyNow()
        recoveryError = URLError(.timedOut); recoveryClock = 2001; recoveryMonitor.beginMonitoring()
        while recoveryPause == nil { await Task.yield() }
        recoveryClock = 2020
        var resumePause = recoveryPause; recoveryPause = nil; resumePause?.resume(); resumePause = nil
        while recoveryPause == nil { await Task.yield() }
        recoveryClock = 2050
        resumePause = recoveryPause; recoveryPause = nil; resumePause?.resume(); resumePause = nil
        while recoveryPause == nil { await Task.yield() }
        check(recoveryMonitor.phase == .unavailable && recoveryMonitor.monitorEnabled && recoveryEvents.last?.requiresExplicitRestart == false, "owned periodic monitor keeps running after bounded transport outage")
        recoveryError = nil; recoveryClock = 2051
        resumePause = recoveryPause; recoveryPause = nil; resumePause?.resume(); resumePause = nil
        while recoveryPause == nil { await Task.yield() }
        check(recoveryMonitor.phase == .ready && recoveryEvents.last?.reconnectVerifiedSession == true && recoveryMonitor.ownership == recoveryOwner, "periodic exact200 proof passively recovers same session without explicit restart")
        recoveryMonitor.endMonitoring(); recoveryPause?.resume(throwing: CancellationError()); recoveryPause = nil
        _ = recoveryMonitor.beginStop(recoveryOwner); _ = recoveryMonitor.completeStop(recoveryOwner)
        print("NativeRuntimeHealthFeatureTests: \(count) assertions passed")
    }
}
