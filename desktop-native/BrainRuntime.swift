import Foundation
import Darwin

@MainActor final class BrainRuntime {
    private var child: Process?
    private var logHandle: FileHandle?
    private var lifeline: Pipe?
    private(set) var baseURL = URL(string: "http://127.0.0.1:9465")!
    private(set) var logURL: URL?
    private(set) var profileConfigRoot: URL?
    private(set) var profileDataRoot: URL?
    private var identity = UUID().uuidString
    private var lifecycleBusy = false
    private var stopRequested = false
    private(set) var ownership: NativeRuntimeOwnership?
    // A cleared owner alone is not proof that its exact Process was retired.
    // Keep the completed stop identity until a new launch begins.
    private var quiescedOwnership: NativeRuntimeOwnership?
    var onHealthEvent: ((NativeRuntimeHealthEvent) -> Void)?
    private lazy var health = NativeRuntimeHealthCoordinator(probe: { [weak self] owner in
        guard let self, self.ownership == owner else { throw NativeFailure("The owned runtime changed before probing.") }
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 5; config.timeoutIntervalForResource = 8
        let session = URLSession(configuration: config, delegate: BrainHealthRedirectGuard(), delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        let started = ProcessInfo.processInfo.systemUptime
        do {
            let (data, response) = try await session.data(from: owner.baseURL.appendingPathComponent("health"))
            guard let http = response as? HTTPURLResponse, http.url == owner.baseURL.appendingPathComponent("health") else { throw NativeFailure("The owned health response was not verified.") }
            let header = http.value(forHTTPHeaderField: "X-Feral-Desktop-Instance")
            let state = NativeRuntimeReadinessWire.agentReadiness(data.count <= 262_144 ? ((try? JSONSerialization.jsonObject(with: data)) ?? [:]) : [:])
            let code = header != nil && header != owner.instanceID ? "foreign_instance" : (header == nil ? "missing_instance" : (http.statusCode != 200 ? "http_failure" : (state.ready ? "ready" : "limited")))
            self.recordHealthProbe(owner, code: code, duration: ProcessInfo.processInfo.systemUptime - started)
            return NativeRuntimeProbeResult(statusCode: http.statusCode, instanceHeader: header, agentReady: state.ready, readinessReason: state.reason)
        } catch {
            self.recordHealthProbe(owner, code: NativeRuntimeProbeDiagnostics.transportCode(error) ?? "probe_protocol_error", duration: ProcessInfo.processInfo.systemUptime - started)
            throw error
        }
    }, isRunning: { [weak self] owner in
        guard let self, self.ownership == owner else { return false }
        return self.child?.isRunning == true
    }, onEvent: { [weak self] event in
        guard let self, self.ownership == event.ownership else { return }
        self.onHealthEvent?(event)
    })
    private func recordHealthProbe(_ owner: NativeRuntimeOwnership, code: String, duration: Double) {
        guard ownership == owner, duration.isFinite, duration >= 0, let logHandle else { return }
        let line = "native_runtime_health code=" + code + " duration_ms=" + String(Int(min(duration * 1000, 86_400_000))) + " process_running=" + String(child?.isRunning == true) + "\n"
        try? logHandle.write(contentsOf: Data(line.utf8))
    }
    private enum StartupPhase: String {
        case processRunEntered = "process_run_entered", processRunReturned = "process_run_returned"
        case initialHealthBegin = "initial_health_begin", initialHealthResponse = "initial_health_response", initialHealthError = "initial_health_error"
        case ownedVerifyBegin = "owned_verify_begin", ownedVerifyReturned = "owned_verify_returned"
    }
    private func recordStartup(_ owner: NativeRuntimeOwnership, phase: StartupPhase, attempt: Int = 0, code: String = "none") {
        let codes = ["none", "verified", "unverified", "matching_instance", "foreign_instance", "missing_instance", "http_failure",
                     "timeout", "connection_unavailable", "connection_lost", "network_unavailable", "local_transport_policy_refused", "transport_other"]
        guard ownership == owner, (0...120).contains(attempt), codes.contains(code), let logHandle else { return }
        let line = "native_runtime_startup phase=" + phase.rawValue + " attempt=" + String(attempt) + " code=" + code + " process_running=" + String(child?.isRunning == true) + "\n"
        try? logHandle.write(contentsOf: Data(line.utf8))
    }
    var healthWarning: String? { health.healthWarning }
    var serviceReachable: Bool { health.serviceReachable && child?.isRunning == true }
    var isReady: Bool { health.availableForActions && child?.isRunning == true }
    func isCurrent(_ owner: NativeRuntimeOwnership) -> Bool { ownership == owner }
    func isQuiesced(_ owner: NativeRuntimeOwnership) -> Bool {
        quiescedOwnership == owner && ownership == nil && child == nil && !lifecycleBusy
    }
    func stop(ifOwnedBy owner: NativeRuntimeOwnership) async throws {
        guard !lifecycleBusy, ownership == owner, child != nil else {
            throw NativeFailure("The reviewed local runtime changed before shutdown.")
        }
        lifecycleBusy = true; stopRequested = true
        defer { lifecycleBusy = false }
        await retireCapturedRuntime()
        guard quiescedOwnership == owner, ownership == nil, child == nil else {
            throw NativeFailure("Shutdown of the reviewed local runtime was not confirmed.")
        }
    }
    func sleep() { health.sleep() }
    func wake() async { _ = await health.wake() }


    func start(progress: (String) -> Void) async throws {
        guard !lifecycleBusy else { throw NativeFailure("A local runtime lifecycle operation is already in progress.") }
        if serviceReachable { return }
        lifecycleBusy = true; stopRequested = false
        defer { lifecycleBusy = false }
        if child != nil { await retireCapturedRuntime() }
        do {
        quiescedOwnership = nil
        identity = UUID().uuidString
        guard let resources = Bundle.main.resourceURL else { throw NativeFailure("The app resources are missing.") }
        let python = resources.appendingPathComponent("python/bin/python3")
        let core = resources.appendingPathComponent("feral-core")
        let engine = resources.appendingPathComponent("opencode/bin/opencode")
        guard FileManager.default.isExecutableFile(atPath: python.path), FileManager.default.fileExists(atPath: core.appendingPathComponent("api/server.py").path) else {
            throw NativeFailure("The bundled backend is missing. Reinstall the complete app.")
        }
        let layout = try NativeProfileLayoutFeature.resolve(
            environment: ProcessInfo.processInfo.environment,
            userHome: FileManager.default.homeDirectoryForCurrentUser,
            inspect: nativeProfilePathKind)
        var env = layout.backendEnvironment
        let home = layout.configRoot
        profileConfigRoot = layout.configRoot
        profileDataRoot = layout.dataRoot
        let number = try NativeRuntimePortFeature.resolve(environment: env) {
            try NativeRuntimePortFeature.readSavedSettings(at: home.appendingPathComponent("settings.json"))
        }
        let port = String(number)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        logURL = home.appendingPathComponent("native-desktop.log")
        FileManager.default.createFile(atPath: logURL!.path, contents: nil)
        logHandle = try FileHandle(forWritingTo: logURL!)
        baseURL = URL(string: "http://127.0.0.1:\(number)")!
        env["FERAL_PORT"] = port
        // The reviewed Access setting owns the listener profile. Inherited
        // shell overrides must not silently expose it or defeat a saved LAN
        // choice. The backend resolves settings.json, defaulting to loopback.
        env.removeValue(forKey: "FERAL_HOST")
        env.removeValue(forKey: "FERAL_BIND_HOST")
        env["FERAL_DESKTOP_INSTANCE_ID"] = identity
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONPATH"] = core.path
        env["PATH"] = resources.appendingPathComponent("python/bin").path + ":" + resources.appendingPathComponent("opencode/bin").path + ":" + (env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
        if env["FERAL_OPENCODE_BIN"] == nil { env["FERAL_OPENCODE_BIN"] = engine.path }
        let process = Process()
        process.executableURL = python
        // A separate process group contains this backend and its coding children.
        process.arguments = [resources.appendingPathComponent("native_backend_launcher.py").path]
        process.currentDirectoryURL = core
        process.environment = env
        process.standardOutput = logHandle
        process.standardError = logHandle
        let pipe = Pipe()
        _ = fcntl(pipe.fileHandleForWriting.fileDescriptor, F_SETFD, FD_CLOEXEC)
        lifeline = pipe
        process.standardInput = pipe.fileHandleForReading
        child = process
        let owner = try health.prepare(instanceID: identity, baseURL: baseURL, processIdentity: UUID())
        ownership = owner
        process.terminationHandler = { [weak self] exited in
            Task { @MainActor in
                guard let self, self.child === exited, self.ownership == owner else { return }
                self.health.processExited(owner)
            }
        }
        progress("Starting your local agent…")
        recordStartup(owner, phase: .processRunEntered)
        try process.run()
        recordStartup(owner, phase: .processRunReturned)
        health.didLaunch(owner)
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 2
        let session = URLSession(configuration: config, delegate: BrainHealthRedirectGuard(), delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        let deadline = ProcessInfo.processInfo.systemUptime + 60
        var attempt = 0
        while ProcessInfo.processInfo.systemUptime < deadline {
            guard !stopRequested, !Task.isCancelled else { throw NativeFailure("Local startup was cancelled by shutdown.") }
            guard process.isRunning else {
                try? logHandle?.synchronize()
                let data = (try? Data(contentsOf: logURL!)) ?? Data()
                let tail = String(decoding: data.suffix(6000), as: UTF8.self)
                let status = process.terminationStatus
                throw NativeFailure("The local agent exited (code \(status)).\n\(tail)")
            }
            recordStartup(owner, phase: .initialHealthBegin, attempt: attempt)
            var initialHealthVerified = false
            do {
                let (_, response) = try await session.data(from: baseURL.appendingPathComponent("health"))
                let http = response as? HTTPURLResponse
                let header = http?.value(forHTTPHeaderField: "X-Feral-Desktop-Instance")
                let code = http?.statusCode != 200 ? "http_failure" : (header == nil ? "missing_instance" : (header == identity ? "matching_instance" : "foreign_instance"))
                recordStartup(owner, phase: .initialHealthResponse, attempt: attempt, code: code)
                initialHealthVerified = http?.statusCode == 200 && header == identity && process.isRunning
            } catch {
                let code = (error as? URLError)?.code == .appTransportSecurityRequiresSecureConnection ? "local_transport_policy_refused" : (NativeRuntimeProbeDiagnostics.transportCode(error) ?? "transport_other")
                recordStartup(owner, phase: .initialHealthError, attempt: attempt, code: code)
            }
            if initialHealthVerified {
                recordStartup(owner, phase: .ownedVerifyBegin, attempt: attempt)
                let verified = await health.verifyNow()
                recordStartup(owner, phase: .ownedVerifyReturned, attempt: attempt, code: verified ? "verified" : "unverified")
                guard verified, ownership == owner, !stopRequested, !Task.isCancelled else { throw NativeFailure("The owned runtime health identity changed at startup.") }
                health.beginMonitoring()
                progress(health.availableForActions ? "Ready on this Mac" : "Local service connected. Full agent startup is paused; use Security for recovery.")
                return
            }
            if attempt == 15 { progress("Preparing local storage. First launch can take longer…") }
            attempt += 1
            try await Task.sleep(nanoseconds: 500_000_000)
        }
        throw NativeFailure("The local agent did not start within 60 seconds. Its startup log is at \(logURL!.path).")
        } catch {
            if let owner = ownership, health.phase == .starting { health.launchFailed(owner) }
            await retireCapturedRuntime()
            throw error
        }
    }

    func stop() async {
        // A replacement cannot start until the captured cleanup completes.
        stopRequested = true
        while lifecycleBusy { try? await Task.sleep(nanoseconds: 20_000_000) }
        lifecycleBusy = true
        defer { lifecycleBusy = false }
        await retireCapturedRuntime()
    }
    private func retireCapturedRuntime() async {
        guard let process = child else {
            try? logHandle?.close(); try? lifeline?.fileHandleForWriting.close(); try? lifeline?.fileHandleForReading.close()
            logHandle = nil; lifeline = nil
            return
        }
        let owner = ownership, capturedLog = logHandle, capturedPipe = lifeline
        if let owner { _ = health.beginStop(owner) }
        process.terminationHandler = nil
        let pid = process.processIdentifier
        // Never signal a PID/group after the exact Process has already exited.
        // Earlier backend-dispatched actions may have independent effects.
        if process.isRunning, pid > 0 { kill(-pid, SIGTERM) }
        for _ in 0..<30 {
            if !process.isRunning { break }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        if process.isRunning, pid > 0 { kill(-pid, SIGKILL) }
        if process.isRunning { process.waitUntilExit() }
        guard !process.isRunning else { return }
        try? capturedLog?.close()
        try? capturedPipe?.fileHandleForWriting.close()
        try? capturedPipe?.fileHandleForReading.close()
        guard child === process, ownership == owner else { return }
        logHandle = nil; lifeline = nil; child = nil
        if let owner { _ = health.completeStop(owner) }
        ownership = nil
        quiescedOwnership = owner
    }

}

private final class BrainHealthRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}

private func nativeProfilePathKind(_ url: URL) -> NativeProfilePathKind {
    var metadata = stat()
    guard lstat(url.path, &metadata) == 0 else {
        return errno == ENOENT ? .missing : .unavailable
    }
    switch metadata.st_mode & S_IFMT {
    case S_IFDIR: return .directory
    case S_IFLNK: return .symbolicLink
    default: return .other
    }
}

struct NativeFailure: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
