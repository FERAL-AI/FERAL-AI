import Foundation
import Darwin

/// Run only through the disposable minimal-bundle acceptance fixture. This
/// exercises the production runtime and launcher, not a mocked owner callback.
@main struct NativeArchiveRuntimeOwnershipTests {
    @MainActor static var assertions = 0
    @MainActor static func check(_ value: @autoclosure () -> Bool, _ message: String) throws {
        guard value() else { throw NativeFailure("Archive ownership fixture assertion failed: " + message) }
        assertions += 1
    }
    @MainActor static func refuses(_ operation: () async throws -> Void) async throws {
        do { try await operation() }
        catch is NativeFailure { assertions += 1; return }
        throw NativeFailure("The runtime accepted a foreign or stale archive stop owner.")
    }
    struct Observed {
        let owner: NativeRuntimeOwnership
        let pid: Int32
        let childPID: Int32
        var evidence: [String: Any] {
            ["generation": owner.generation.uuidString, "instance_id": owner.instanceID,
             "process_identity": owner.processIdentity.uuidString, "base_url": owner.baseURL.absoluteString,
             "backend_pid": pid, "owned_child_pid": childPID]
        }
    }
    static func alive(_ pid: Int32) -> Bool { pid > 1 && kill(pid, 0) == 0 }
    static func absent(_ pid: Int32) -> Bool { pid > 1 && kill(pid, 0) != 0 && errno == ESRCH }
    static func listenerAbsent(_ origin: URL) -> Bool {
        guard origin.host == "127.0.0.1", let port = origin.port, let number = UInt16(exactly: port), number > 0 else { return false }
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }; defer { close(fd) }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size); address.sin_family = sa_family_t(AF_INET)
        address.sin_port = number.bigEndian; address.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))
        let result = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        return result != 0 && errno == ECONNREFUSED
    }
    @MainActor static func observe(_ runtime: BrainRuntime, profile: String) async throws -> Observed {
        guard let owner = runtime.ownership else { throw NativeFailure("No actual owned runtime was published.") }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 1; configuration.timeoutIntervalForResource = 2
        let session = URLSession(configuration: configuration); defer { session.invalidateAndCancel() }
        let (bytes, response) = try await session.data(from: owner.baseURL.appendingPathComponent("health"))
        guard bytes.count <= 8192, let http = response as? HTTPURLResponse, http.statusCode == 200,
              http.value(forHTTPHeaderField: "X-Feral-Desktop-Instance") == owner.instanceID,
              let body = try JSONSerialization.jsonObject(with: bytes) as? [String: Any],
              body["fixture_role"] as? String == "health-only",
              let pid = body["fixture_pid"] as? Int32, let child = body["fixture_child_pid"] as? Int32,
              body["fixture_pgrp"] as? Int32 == pid, body["fixture_child_pgrp"] as? Int32 == pid,
              body["fixture_home"] as? String == profile, body["fixture_data_home"] as? String == profile,
              alive(pid), alive(child), pid != child else { throw NativeFailure("The fixture process, listener and launch environment were not verified.") }
        try check(runtime.isReady && runtime.serviceReachable && runtime.isCurrent(owner), "production health coordinator verified owned health")
        return Observed(owner: owner, pid: pid, childPID: child)
    }
    @MainActor static func verifyAbsent(_ observed: Observed) async throws {
        let deadline = ProcessInfo.processInfo.systemUptime + 3
        while ProcessInfo.processInfo.systemUptime < deadline {
            if absent(observed.pid), absent(observed.childPID), listenerAbsent(observed.owner.baseURL) { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        try check(absent(observed.pid), "actual backend Process is absent")
        try check(absent(observed.childPID), "actual owned process-group child is absent")
        try check(listenerAbsent(observed.owner.baseURL), "captured loopback listener refuses connections")
    }
    @MainActor static func main() async throws {
        let env = ProcessInfo.processInfo.environment
        guard Bundle.main.bundleIdentifier == "ai.feral.fixture.archive-runtime-owner",
              let root = env["FERAL_ARCHIVE_OWNER_FIXTURE_ROOT"], root.hasPrefix("/private/tmp/feral-native-archive-owner-"),
              let home = env["FERAL_HOME"], home == root + "/profile", env["FERAL_DATA_HOME"] == home,
              let resources = Bundle.main.resourceURL, resources.path == root + "/ArchiveOwnerFixture.app/Contents/Resources"
        else { throw NativeFailure("This test requires the exact disposable bundle fixture; personal profiles are never selected.") }
        guard let canonical = realpath(root, nil) else { throw NativeFailure("Fixture root is unavailable.") }
        defer { free(canonical) }
        guard String(cString: canonical) == root else { throw NativeFailure("Fixture root was redirected.") }
        let runtime = BrainRuntime(); var events: [NativeRuntimeHealthEvent] = [], progress: [String] = []
        runtime.onHealthEvent = { events.append($0) }
        do {
            try await runtime.start { progress.append($0) }
            let first = try await observe(runtime, profile: home)
            try check(runtime.profileConfigRoot?.path == home && runtime.profileDataRoot?.path == home, "actual runtime uses reviewed same-root backend policy")
            try check(!runtime.isQuiesced(first.owner), "a running owner never has archive stop proof")
            let wrong = NativeRuntimeOwnership(generation: UUID(), instanceID: first.owner.instanceID,
                baseURL: first.owner.baseURL, processIdentity: first.owner.processIdentity)
            let before = events.count
            try await refuses { try await runtime.stop(ifOwnedBy: wrong) }
            let stillFirst = try await observe(runtime, profile: home)
            try check(stillFirst.owner == first.owner && stillFirst.pid == first.pid && stillFirst.childPID == first.childPID,
                      "wrong-owner stop leaves exact original backend and child alive")
            try check(events.count == before, "wrong-owner stop emits no lifecycle retirement")
            try check(!runtime.isQuiesced(wrong) && !runtime.isQuiesced(first.owner), "wrong owner cannot forge quiescence")
            try await runtime.stop(ifOwnedBy: first.owner)
            try await verifyAbsent(first)
            try check(runtime.ownership == nil && runtime.isQuiesced(first.owner), "exact stop publishes proof only after actual cleanup")
            try check(!runtime.isQuiesced(wrong) && !runtime.serviceReachable && !runtime.isReady, "cold proof is exact and action readiness revoked")
            try await refuses { try await runtime.stop(ifOwnedBy: first.owner) }
            try check(runtime.isQuiesced(first.owner), "repeated exact stop refusal preserves settled proof without replay")

            try await runtime.start { progress.append($0) }
            let second = try await observe(runtime, profile: home)
            try check(second.owner != first.owner && second.owner.generation != first.owner.generation,
                      "new real launch owns a new lifecycle generation")
            try check(second.owner.processIdentity != first.owner.processIdentity && second.owner.instanceID != first.owner.instanceID,
                      "new launch has distinct process and health identities")
            try check(!runtime.isQuiesced(first.owner) && !runtime.isQuiesced(second.owner), "new launch invalidates prior stopped proof")
            try await refuses { try await runtime.stop(ifOwnedBy: first.owner) }
            let stillSecond = try await observe(runtime, profile: home)
            try check(stillSecond.owner == second.owner && stillSecond.pid == second.pid && stillSecond.childPID == second.childPID,
                      "stale stop cannot kill replacement backend or owned child")
            try await runtime.stop(ifOwnedBy: second.owner)
            try await verifyAbsent(second)
            try check(runtime.isQuiesced(second.owner) && !runtime.isQuiesced(first.owner), "only exact replacement cleanup establishes current cold proof")
            try check(events.allSatisfy { !$0.automaticActionReplay }, "real lifecycle events never request replay")
            try check(events.contains(where: { $0.phase == .ready }) && events.contains(where: { $0.phase == .stopped }), "production coordinator emitted observed ready and stopped events")
            let evidence: [String: Any] = ["status": "passed", "assertions": assertions, "bundle_resources_verified": true,
                "fixture": "health-only-no-providers", "first": first.evidence, "replacement": second.evidence,
                "backend_and_child_absent": true, "listeners_absent": true, "exact_quiescence_verified": true,
                "native_model_archive_host_verified": false, "signed_app_verified": false, "runtime_started_automatically_after_stop": false,
                "observed_phases": events.map { $0.phase.rawValue }, "progress": progress]
            let output = try JSONSerialization.data(withJSONObject: evidence, options: [.sortedKeys])
            print(String(decoding: output, as: UTF8.self))
        } catch {
            // This runtime exists only inside the isolated fixture bundle.
            await runtime.stop(); throw error
        }
    }
}
