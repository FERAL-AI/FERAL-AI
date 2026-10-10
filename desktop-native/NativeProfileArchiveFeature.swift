import Foundation
import Combine
import CryptoKit
import Darwin

enum NativeProfileArchiveFailure: String, LocalizedError {
    case invalid, busy, changed, occupied, otherWriters, unavailable, unconfirmed
    var errorDescription: String? {
        switch self {
        case .invalid: return "The selected archive, profile or migration scope is unsupported."
        case .busy: return "An archive operation or review is already in progress."
        case .changed: return "The reviewed profile, runtime owner or archive changed. Prepare a new review."
        case .occupied: return "Choose a new archive, new profile roots and a fresh preference suite. Existing data was not replaced."
        case .otherWriters: return "Confirm that all other writers for the selected profiles are stopped. Stopping this app cannot prove that."
        case .unavailable: return "The archive operation could not start. No runtime was restarted."
        case .unconfirmed: return "The archive operation was not confirmed. Inspect the selected destination before any further migration; no replay, reset or activation was performed."
        }
    }
}

enum NativeProfileArchiveOwnerStatus { case current, quiesced, superseded }

struct NativeProfileArchiveScope {
    let configRoot: URL
    let dataRoot: URL
    let defaults: UserDefaults
    let suiteName: String
    let primarySessionID: String
    let runtimeOwner: NativeRuntimeOwnership
}

struct NativeProfileArchiveTarget {
    let configRoot: URL
    let dataRoot: URL
    let defaults: UserDefaults
    let suiteName: String
    let primarySessionID: String
    var nativeLayoutCompatible: Bool { configRoot.path == dataRoot.path }
}

struct NativeProfileArchiveReview: Equatable, Identifiable {
    enum Operation { case backup, restore }
    let id: UUID
    let operation: Operation
    let archiveURL: URL
    let configRoot: URL
    let dataRoot: URL
    let suiteName: String
    let primarySessionID: String
    let runtimeOwner: NativeRuntimeOwnership
    let preferenceKeys: [String]
    let includesAvatar: Bool
    let archiveSHA256: String?
    let createdUptime: TimeInterval
    let otherWritersStopped: Bool
    var nativeLayoutCompatible: Bool { configRoot.path == dataRoot.path }
    static let disclosure = "This stops this app's captured local runtime. You must stop all other profile writers separately. Archives contain the selected profile's regular files, including encrypted artifacts; OS keys are not portable. Restore creates new roots only. Preferences need a second confirmation. No tasks, purchases, messages, pairing activation or runtime restart are performed."
}

struct NativeProfileArchiveApplyReview: Equatable, Identifiable {
    let id: UUID
    let archiveURL: URL
    let configRoot: URL
    let dataRoot: URL
    let primarySessionID: String
    let suiteName: String
    let preferenceKeys: [String]
    let includesAvatar: Bool
    let snapshotSHA256: String
    let createdUptime: TimeInterval
    var nativeLayoutCompatible: Bool { configRoot.path == dataRoot.path }
}

struct NativeProfileArchiveReceipt {
    enum Operation { case backup, restored, preferencesApplied }
    let operation: Operation
    let archiveURL: URL
    let archiveSHA256: String
    let configRoot: URL
    let dataRoot: URL
    let primarySessionID: String
    let suiteName: String
    let snapshotSHA256: String
    let files: Int64
    let bytes: Int64
    let preferencesApplied: Bool
    let preferenceReadbackVerified: Bool
    let runtimeStarted = false
    let osKeysIncluded = false
    var nativeLayoutCompatible: Bool { configRoot.path == dataRoot.path }
}

struct NativeProfileArchiveCommand {
    enum Operation { case backup, restore }
    let operation: Operation
    let archiveURL: URL
    let configRoot: URL
    let dataRoot: URL
    let primarySessionID: String
    var snapshotURL: URL? = nil
    var snapshotSHA256: String? = nil
    var arguments: [String] {
        var result = ["-s", "-m", "config.profile_archive", operation == .backup ? "backup" : "restore",
                      archiveURL.path, "--offline", "--config-root", configRoot.path,
                      "--data-root", dataRoot.path, "--primary-session-id", primarySessionID]
        if operation == .backup, let snapshotURL, let snapshotSHA256 {
            result += ["--native-preferences", snapshotURL.path, "--native-preferences-sha256", snapshotSHA256]
        }
        return result
    }
}

/// No shell, inherited credentials, OS defaults or application activation.
/// The enclosing signed app supplies its bundled interpreter and core directory.
struct NativeProfileArchiveProcessExecutor {
    let pythonURL: URL
    let coreURL: URL
    var timeout: TimeInterval = 120
    var maximumOutputBytes = 65_536
    static func bundled(resources: URL? = Bundle.main.resourceURL) throws -> Self {
        guard let resources else { throw NativeProfileArchiveFailure.unavailable }
        return Self(pythonURL: resources.appendingPathComponent("python/bin/python3"),
                    coreURL: resources.appendingPathComponent("feral-core"))
    }
    func execute(_ command: NativeProfileArchiveCommand) async throws -> Data {
        guard pythonURL.isFileURL, coreURL.isFileURL, timeout.isFinite, timeout > 0, timeout <= 600,
              maximumOutputBytes > 0, maximumOutputBytes <= 262_144,
              FileManager.default.isExecutableFile(atPath: pythonURL.path),
              FileManager.default.fileExists(atPath: coreURL.appendingPathComponent("config/profile_archive.py").path)
        else { throw NativeProfileArchiveFailure.unavailable }
        let worker = NativeProfileArchiveProcessWorker()
        return try await withTaskCancellationHandler(operation: {
            try Task.checkCancellation()
            return try await Task.detached(priority: .utility) {
                try worker.run(python: pythonURL, core: coreURL, command: command,
                               timeout: timeout, limit: maximumOutputBytes)
            }.value
        }, onCancel: { worker.cancel() })
    }
}

private final class NativeProfileArchiveProcessWorker: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?
    private var cancelled = false
    func cancel() {
        lock.lock(); cancelled = true; let active = process; lock.unlock()
        if let active, active.isRunning { active.terminate() }
    }
    private func isCancelled() -> Bool { lock.lock(); defer { lock.unlock() }; return cancelled }
    func run(python: URL, core: URL, command: NativeProfileArchiveCommand,
             timeout: Double, limit: Int) throws -> Data {
        let child = Process(), out = Pipe(), err = Pipe()
        child.executableURL = python; child.arguments = command.arguments; child.currentDirectoryURL = core
        child.environment = ["PATH": python.deletingLastPathComponent().path + ":/usr/bin:/bin",
                             "PYTHONPATH": core.path, "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                             "FERAL_HOME": command.configRoot.path, "FERAL_DATA_HOME": command.dataRoot.path,
                             "PYTHONUNBUFFERED": "1"]
        child.standardInput = FileHandle.nullDevice; child.standardOutput = out; child.standardError = err
        lock.lock(); process = child; let stop = cancelled; lock.unlock()
        defer { lock.lock(); process = nil; lock.unlock() }
        guard !stop else { throw NativeProfileArchiveFailure.unavailable }
        do { try child.run() } catch { throw NativeProfileArchiveFailure.unavailable }
        try? out.fileHandleForWriting.close(); try? err.fileHandleForWriting.close()
        let captured = NativeProfileArchiveOutput(limit: limit), readers = DispatchGroup()
        for (handle, stdout) in [(out.fileHandleForReading, true), (err.fileHandleForReading, false)] {
            readers.enter()
            DispatchQueue.global(qos: .utility).async {
                defer { try? handle.close(); readers.leave() }
                let fd = handle.fileDescriptor, flags = fcntl(handle.fileDescriptor, F_GETFL)
                guard flags >= 0, fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0 else { captured.failed(); return }
                var buffer = [UInt8](repeating: 0, count: 16_384)
                while true {
                    let count = Darwin.read(fd, &buffer, buffer.count)
                    if count > 0 { captured.append(Data(buffer[0..<count]), stdout: stdout) }
                    else if count == 0 { break }
                    else if errno == EINTR { continue }
                    else if errno == EAGAIN || errno == EWOULDBLOCK {
                        // Once our exact child exits, drain its buffered bytes
                        // without waiting for an inherited pipe in another process.
                        if captured.readingFinished { break }; usleep(1_000)
                    } else {
                        captured.failed(); break
                    }
                }
            }
        }
        let deadline = ProcessInfo.processInfo.systemUptime + timeout
        var interrupted = false
        while child.isRunning {
            if isCancelled() || captured.exceeded || ProcessInfo.processInfo.systemUptime >= deadline {
                interrupted = true; child.terminate()
                let grace = ProcessInfo.processInfo.systemUptime + 0.5
                while child.isRunning && ProcessInfo.processInfo.systemUptime < grace { usleep(10_000) }
                if child.isRunning { kill(child.processIdentifier, SIGKILL) }
                break
            }
            usleep(10_000)
        }
        child.waitUntilExit()
        captured.finishReading()
        let drained = readers.wait(timeout: .now() + 1) == .success
        guard drained, !interrupted, !isCancelled(), !captured.exceeded,
              child.terminationReason == .exit, child.terminationStatus == 0 else {
            throw NativeProfileArchiveFailure.unconfirmed
        }
        return captured.stdout
    }
}

private final class NativeProfileArchiveOutput: @unchecked Sendable {
    private let lock = NSLock(), limit: Int
    private var output = Data(), stderrBytes = 0, overflow = false, finished = false
    init(limit: Int) { self.limit = limit }
    func append(_ data: Data, stdout: Bool) {
        lock.lock(); defer { lock.unlock() }
        if stdout {
            if output.count + data.count > limit { overflow = true } else { output.append(data) }
        } else { stderrBytes += data.count; if stderrBytes > limit { overflow = true } }
    }
    func failed() { lock.lock(); overflow = true; lock.unlock() }
    func finishReading() { lock.lock(); finished = true; lock.unlock() }
    var readingFinished: Bool { lock.lock(); defer { lock.unlock() }; return finished }
    var exceeded: Bool { lock.lock(); defer { lock.unlock() }; return overflow }
    var stdout: Data { lock.lock(); defer { lock.unlock() }; return output }
}

private struct NativeProfileArchiveFile: Equatable {
    let device: UInt64, inode: UInt64, size: Int64, modified: Int64, modifiedNanos: Int64, changed: Int64, changedNanos: Int64
    let sha256: String
    let contents: Data?
    static func identity(_ value: stat) -> [Int64] {
        [Int64(value.st_dev), Int64(value.st_ino), value.st_size, Int64(value.st_mtimespec.tv_sec),
         Int64(value.st_mtimespec.tv_nsec), Int64(value.st_ctimespec.tv_sec), Int64(value.st_ctimespec.tv_nsec)]
    }
    static func read(_ url: URL, maximum: Int64, capture: Bool = false) throws -> Self {
        try NativeProfileArchivePaths.lexical(url)
        let parent = try NativeProfileArchivePaths.directory(url.deletingLastPathComponent())
        let fd = Darwin.open(url.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { throw NativeProfileArchiveFailure.changed }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        var before = stat(), after = stat(), final = stat()
        guard fstat(fd, &before) == 0, (before.st_mode & S_IFMT) == S_IFREG,
              before.st_size >= 0, before.st_size <= maximum else { throw NativeProfileArchiveFailure.invalid }
        var hash = SHA256(), bytes = Data(), size: Int64 = 0
        do {
            while let chunk = try handle.read(upToCount: 65_536), !chunk.isEmpty {
                try Task.checkCancellation(); size += Int64(chunk.count)
                guard size <= maximum else { throw NativeProfileArchiveFailure.invalid }
                hash.update(data: chunk); if capture { bytes.append(chunk) }
            }
        } catch let error as NativeProfileArchiveFailure { throw error }
        catch { throw NativeProfileArchiveFailure.changed }
        guard size == before.st_size, fstat(fd, &after) == 0, lstat(url.path, &final) == 0,
              identity(before) == identity(after), identity(before) == identity(final),
              try NativeProfileArchivePaths.directory(url.deletingLastPathComponent()) == parent else { throw NativeProfileArchiveFailure.changed }
        return Self(device: UInt64(before.st_dev), inode: before.st_ino, size: before.st_size,
                    modified: Int64(before.st_mtimespec.tv_sec), modifiedNanos: Int64(before.st_mtimespec.tv_nsec),
                    changed: Int64(before.st_ctimespec.tv_sec), changedNanos: Int64(before.st_ctimespec.tv_nsec),
                    sha256: hash.finalize().map { String(format: "%02x", $0) }.joined(), contents: capture ? bytes : nil)
    }
}

private enum NativeProfileArchivePaths {
    static func lexical(_ url: URL) throws {
        let path = url.path, parts = path.split(separator: "/", omittingEmptySubsequences: false)
        guard url.isFileURL, url.host == nil || url.host == "", path.hasPrefix("/"), path.utf8.count <= 4096,
              path == "/" || parts.dropFirst().allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }),
              !path.unicodeScalars.contains(where: { [.control, .format, .surrogate].contains($0.properties.generalCategory) })
        else { throw NativeProfileArchiveFailure.invalid }
    }
    static func directory(_ url: URL) throws -> String {
        try lexical(url)
        guard let resolved = realpath(url.path, nil) else { throw NativeProfileArchiveFailure.changed }
        defer { free(resolved) }
        var info = stat()
        guard String(cString: resolved) == url.path, lstat(url.path, &info) == 0,
              (info.st_mode & S_IFMT) == S_IFDIR else { throw NativeProfileArchiveFailure.changed }
        return "\(info.st_dev):\(info.st_ino)"
    }
    static func fresh(_ url: URL) throws -> String {
        try lexical(url)
        var path = url
        while !FileManager.default.fileExists(atPath: path.path) {
            var info = stat()
            guard lstat(path.path, &info) != 0, errno == ENOENT else { throw NativeProfileArchiveFailure.occupied }
            path = path.deletingLastPathComponent()
        }
        guard path.path != url.path else { throw NativeProfileArchiveFailure.occupied }
        return path.path + ":" + (try directory(path))
    }
    static func outside(_ url: URL, config: URL, data: URL) throws {
        guard !inside(url, config), !inside(url, data) else { throw NativeProfileArchiveFailure.invalid }
    }
    static func inside(_ path: URL, _ parent: URL) -> Bool { path.path == parent.path || path.path.hasPrefix(parent.path + "/") }
    static func layout(config: URL, data: URL) throws {
        try lexical(config); try lexical(data)
        guard config.path == data.path || !inside(config, data) else { throw NativeProfileArchiveFailure.invalid }
    }
}

private enum NativeProfileArchiveReceiptWire {
    static func parse(_ bytes: Data, command: NativeProfileArchiveCommand) throws -> (files: Int64, bytes: Int64, snapshot: String, path: URL?) {
        guard bytes.count <= 65_536 else { throw NativeProfileArchiveFailure.unconfirmed }
        // CLI receipts are flat objects. Reject duplicate keys rather than
        // relying on Foundation's last-key-wins JSON decoder.
        let input = Array(bytes); var offset = 0, keys = Set<String>()
        func space() { while offset < input.count && [9, 10, 13, 32].contains(input[offset]) { offset += 1 } }
        func string() throws -> Data {
            guard offset < input.count, input[offset] == 34 else { throw NativeProfileArchiveFailure.unconfirmed }
            let start = offset; offset += 1; var escaped = false
            while offset < input.count {
                let byte = input[offset]; offset += 1
                if escaped { escaped = false }
                else if byte == 92 { escaped = true }
                else if byte == 34 { return Data(input[start..<offset]) }
            }
            throw NativeProfileArchiveFailure.unconfirmed
        }
        space(); guard offset < input.count, input[offset] == 123 else { throw NativeProfileArchiveFailure.unconfirmed }; offset += 1
        while true {
            space(); let token = try string()
            guard let key = (try? JSONSerialization.jsonObject(with: Data("[".utf8) + token + Data("]".utf8))) as? [String],
                  key.count == 1, keys.insert(key[0]).inserted else { throw NativeProfileArchiveFailure.unconfirmed }
            space(); guard offset < input.count, input[offset] == 58 else { throw NativeProfileArchiveFailure.unconfirmed }; offset += 1; space()
            if offset < input.count, input[offset] == 34 { _ = try string() }
            else {
                while offset < input.count && input[offset] != 44 && input[offset] != 125 {
                    guard input[offset] != 123 && input[offset] != 91 else { throw NativeProfileArchiveFailure.unconfirmed }; offset += 1
                }
            }
            space(); guard offset < input.count else { throw NativeProfileArchiveFailure.unconfirmed }
            if input[offset] == 125 { offset += 1; break }
            guard input[offset] == 44 else { throw NativeProfileArchiveFailure.unconfirmed }; offset += 1
        }
        space(); guard offset == input.count, let body = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any] else { throw NativeProfileArchiveFailure.unconfirmed }
        var expected: Set<String> = ["status", "files", "bytes", "coverage", "native_preferences_included", "os_keys_included", "credentials_portable",
                                     "native_preferences_sha256", "native_primary_session_id", "preferences_applied"]
        if command.operation == .backup { expected.insert("native_preferences_entry") }
        else { expected.formUnion(["runtime_started", "native_preferences_path"]) }
        guard keys == expected, body["status"] as? String == "completed", body["coverage"] as? String == "selected_roots_only",
              NativeContextCheckpointWire.boolean(body["native_preferences_included"]) == true,
              NativeContextCheckpointWire.boolean(body["os_keys_included"]) == false,
              NativeContextCheckpointWire.boolean(body["credentials_portable"]) == false,
              NativeContextCheckpointWire.boolean(body["preferences_applied"]) == false,
              body["native_primary_session_id"] as? String == command.primarySessionID,
              let files = NativeContextCheckpointWire.integer(body["files"], minimum: 1, maximum: 20_000),
              let count = NativeContextCheckpointWire.integer(body["bytes"], minimum: 0, maximum: 1_073_741_824),
              let digest = body["native_preferences_sha256"] as? String,
              digest.count == 64, digest.allSatisfy({ "0123456789abcdef".contains($0) }) else { throw NativeProfileArchiveFailure.unconfirmed }
        if command.operation == .backup {
            guard body["native_preferences_entry"] as? String == "config/.feral-native-preferences.v1.json", digest == command.snapshotSHA256 else { throw NativeProfileArchiveFailure.unconfirmed }
            return (files, count, digest, nil)
        }
        let path = command.configRoot.appendingPathComponent(".feral-native-preferences.v1.json")
        guard body["native_preferences_path"] as? String == path.path, NativeContextCheckpointWire.boolean(body["runtime_started"]) == false else { throw NativeProfileArchiveFailure.unconfirmed }
        return (files, count, digest, path)
    }
}

/// Caller-supplied ownerStatus must retain exact stop ownership. A nil Process
/// alone is not a stop receipt, and this component provides no global writer lease.
@MainActor final class NativeProfileArchiveFeature: ObservableObject {
    enum Phase { case idle, inspectingArchive, reviewingBackup, reviewingRestore, quiescing, backingUp, restoring, reviewingApply, applying, completed, failed, unconfirmed }
    typealias Executor = (NativeProfileArchiveCommand) async throws -> Data
    @Published private(set) var phase: Phase = .idle
    @Published private(set) var review: NativeProfileArchiveReview?
    @Published private(set) var applyReview: NativeProfileArchiveApplyReview?
    @Published private(set) var receipt: NativeProfileArchiveReceipt?
    @Published private(set) var failure: NativeProfileArchiveFailure?
    private let preferences: NativePreferenceArchiveFeature, allowedDestinations: Set<String>
    private let ownerStatus: (NativeRuntimeOwnership) -> NativeProfileArchiveOwnerStatus
    private let quiesce: (NativeRuntimeOwnership) async throws -> Void, executor: Executor, uptime: () -> TimeInterval
    private struct Pending {
        let review: NativeProfileArchiveReview, scope: NativeProfileArchiveScope, target: NativeProfileArchiveTarget?
        let preferences: NativePreferenceArchiveReview, sourceRoots: [String], primary: NativeProfileArchiveFile
        let archive: NativeProfileArchiveFile?, destinationParents: [String]
    }
    private struct Apply {
        let review: NativeProfileArchiveApplyReview, scope: NativeProfileArchiveScope, target: NativeProfileArchiveTarget
        let preferences: NativePreferenceArchiveReview, snapshot: NativeProfileArchiveFile, archive: NativeProfileArchiveFile
        let roots: [String], primary: NativeProfileArchiveFile, receipt: NativeProfileArchiveReceipt
    }
    private var pending: Pending?, apply: Apply?
    @Published private var operation: UUID?
    var busy: Bool { operation != nil }
    init(preferences: NativePreferenceArchiveFeature? = nil, allowedDestinations: Set<String>,
         ownerStatus: @escaping (NativeRuntimeOwnership) -> NativeProfileArchiveOwnerStatus,
         quiesce: @escaping (NativeRuntimeOwnership) async throws -> Void,
         executor: @escaping Executor,
         uptime: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) {
        self.preferences = preferences ?? NativePreferenceArchiveFeature(); self.allowedDestinations = allowedDestinations; self.ownerStatus = ownerStatus
        self.quiesce = quiesce; self.executor = executor; self.uptime = uptime
    }
    private func available() throws {
        guard operation == nil, pending == nil, apply == nil else { throw NativeProfileArchiveFailure.busy }
        guard phase != .unconfirmed else { throw NativeProfileArchiveFailure.unconfirmed }
    }
    private func source(_ scope: NativeProfileArchiveScope) throws -> ([String], NativeProfileArchiveFile) {
        try NativeProfileArchivePaths.layout(config: scope.configRoot, data: scope.dataRoot)
        let roots = try [NativeProfileArchivePaths.directory(scope.configRoot), NativeProfileArchivePaths.directory(scope.dataRoot)]
        let primary = try NativeProfileArchiveFile.read(scope.dataRoot.appendingPathComponent("primary_session_id"), maximum: 1024, capture: true)
        guard NativeContextCheckpointWire.validID(scope.primarySessionID),
              String(data: primary.contents!, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) == scope.primarySessionID else { throw NativeProfileArchiveFailure.changed }
        return (roots, primary)
    }
    private func targetParents(_ target: NativeProfileArchiveTarget) throws -> [String] {
        try NativeProfileArchivePaths.layout(config: target.configRoot, data: target.dataRoot)
        guard (target.suiteName.hasPrefix("ai.feral.") || target.suiteName.hasPrefix("feral.")), target.suiteName.utf8.count <= 128,
              target.suiteName.range(of: "^[A-Za-z0-9._-]+$", options: .regularExpression) != nil else { throw NativeProfileArchiveFailure.invalid }
        guard target.defaults.persistentDomain(forName: target.suiteName) == nil else { throw NativeProfileArchiveFailure.occupied }
        return try [NativeProfileArchivePaths.fresh(target.configRoot), NativeProfileArchivePaths.fresh(target.dataRoot)]
    }
    private func capture(_ scope: NativeProfileArchiveScope) throws -> Data {
        let review = try preferences.prepareExport(defaults: scope.defaults, suiteName: scope.suiteName,
            verifiedPrimary: scope.primarySessionID, profileRoot: scope.configRoot, allowedDestinations: allowedDestinations)
        return try preferences.confirmExport(review, verifiedPrimary: scope.primarySessionID)
    }
    private func check(_ item: Pending, status: NativeProfileArchiveOwnerStatus) throws {
        guard ownerStatus(item.scope.runtimeOwner) == status,
              uptime() >= item.review.createdUptime, uptime() - item.review.createdUptime <= 300,
              !Task.isCancelled else { throw NativeProfileArchiveFailure.changed }
        let (roots, primary) = try source(item.scope)
        guard roots == item.sourceRoots, primary == item.primary else { throw NativeProfileArchiveFailure.changed }
        if let target = item.target { guard try targetParents(target) == item.destinationParents else { throw NativeProfileArchiveFailure.changed } }
        else { guard try NativeProfileArchivePaths.fresh(item.review.archiveURL) == item.destinationParents[0] else { throw NativeProfileArchiveFailure.changed } }
    }
    func prepareBackup(scope: NativeProfileArchiveScope, archiveURL: URL, otherWritersStopped: Bool) throws -> NativeProfileArchiveReview {
        try available(); guard otherWritersStopped else { throw NativeProfileArchiveFailure.otherWriters }
        guard ownerStatus(scope.runtimeOwner) == .current else { throw NativeProfileArchiveFailure.changed }
        let (roots, primary) = try source(scope)
        try NativeProfileArchivePaths.outside(archiveURL, config: scope.configRoot, data: scope.dataRoot)
        let parent = try NativeProfileArchivePaths.fresh(archiveURL)
        _ = try NativeProfileArchivePaths.directory(archiveURL.deletingLastPathComponent())
        let prefs = try preferences.prepareExport(defaults: scope.defaults, suiteName: scope.suiteName,
            verifiedPrimary: scope.primarySessionID, profileRoot: scope.configRoot, allowedDestinations: allowedDestinations)
        let item = NativeProfileArchiveReview(id: UUID(), operation: .backup, archiveURL: archiveURL, configRoot: scope.configRoot,
            dataRoot: scope.dataRoot, suiteName: scope.suiteName, primarySessionID: scope.primarySessionID, runtimeOwner: scope.runtimeOwner,
            preferenceKeys: prefs.preferenceKeys, includesAvatar: prefs.includesAvatar, archiveSHA256: nil, createdUptime: uptime(), otherWritersStopped: true)
        pending = Pending(review: item, scope: scope, target: nil, preferences: prefs, sourceRoots: roots, primary: primary, archive: nil, destinationParents: [parent])
        review = item; receipt = nil; failure = nil; phase = .reviewingBackup; return item
    }
    func prepareRestore(scope: NativeProfileArchiveScope, archiveURL: URL, target: NativeProfileArchiveTarget,
                        otherWritersStopped: Bool) async throws -> NativeProfileArchiveReview {
        try available(); guard otherWritersStopped else { throw NativeProfileArchiveFailure.otherWriters }
        guard ownerStatus(scope.runtimeOwner) == .current, NativeContextCheckpointWire.validID(target.primarySessionID) else { throw NativeProfileArchiveFailure.changed }
        let (roots, primary) = try source(scope), parents = try targetParents(target)
        try NativeProfileArchivePaths.outside(target.configRoot, config: scope.configRoot, data: scope.dataRoot)
        try NativeProfileArchivePaths.outside(target.dataRoot, config: scope.configRoot, data: scope.dataRoot)
        try NativeProfileArchivePaths.outside(archiveURL, config: target.configRoot, data: target.dataRoot)
        _ = try NativeProfileArchivePaths.directory(archiveURL.deletingLastPathComponent())
        let prefs = try preferences.prepareExport(defaults: scope.defaults, suiteName: scope.suiteName,
            verifiedPrimary: scope.primarySessionID, profileRoot: scope.configRoot, allowedDestinations: allowedDestinations)
        operation = UUID(); phase = .inspectingArchive; failure = nil
        defer { operation = nil }
        do {
            let archive = try await Task.detached(priority: .utility) { try NativeProfileArchiveFile.read(archiveURL, maximum: 1_242_710_016) }.value
            guard ownerStatus(scope.runtimeOwner) == .current, !Task.isCancelled else { throw NativeProfileArchiveFailure.changed }
            let item = NativeProfileArchiveReview(id: UUID(), operation: .restore, archiveURL: archiveURL,
                configRoot: target.configRoot, dataRoot: target.dataRoot, suiteName: target.suiteName, primarySessionID: target.primarySessionID,
                runtimeOwner: scope.runtimeOwner, preferenceKeys: [], includesAvatar: false, archiveSHA256: archive.sha256, createdUptime: uptime(), otherWritersStopped: true)
            let value = Pending(review: item, scope: scope, target: target, preferences: prefs, sourceRoots: roots, primary: primary, archive: archive, destinationParents: parents)
            try check(value, status: .current); pending = value; review = item; receipt = nil; failure = nil; phase = .reviewingRestore; return item
        } catch { preferences.cancel(prefs); throw fail(error, dispatched: false) }
    }
    func cancelReview() throws {
        guard !busy else { throw NativeProfileArchiveFailure.busy }
        if let pending { preferences.cancel(pending.preferences) }
        if let apply { preferences.cancel(apply.preferences) }
        pending = nil; apply = nil; review = nil; applyReview = nil
        if phase != .unconfirmed { phase = .idle }
    }
    private func take(_ review: NativeProfileArchiveReview) throws -> Pending {
        guard !busy else { throw NativeProfileArchiveFailure.busy }
        guard let item = pending, item.review == review else { throw NativeProfileArchiveFailure.changed }
        pending = nil; self.review = nil; return item
    }
    private func fail(_ error: Error, dispatched: Bool) -> NativeProfileArchiveFailure {
        let value = dispatched ? NativeProfileArchiveFailure.unconfirmed : ((error as? NativeProfileArchiveFailure) ?? .changed)
        failure = value; phase = value == .unconfirmed ? .unconfirmed : .failed; return value
    }
    func confirmBackup(_ review: NativeProfileArchiveReview) async throws -> NativeProfileArchiveReceipt {
        let item = try take(review); guard review.operation == .backup else { throw NativeProfileArchiveFailure.invalid }
        let op = UUID(); operation = op; defer { operation = nil }
        var dispatched = false, temporary: URL?, tempIdentity: String?, snapshotFile: NativeProfileArchiveFile?
        defer {
            preferences.cancel(item.preferences)
            if let temporary, let tempIdentity, (try? NativeProfileArchivePaths.directory(temporary)) == tempIdentity {
                let file = temporary.appendingPathComponent("native.json")
                if let snapshotFile, (try? NativeProfileArchiveFile.read(file, maximum: 1_048_576, capture: true)) == snapshotFile { try? FileManager.default.removeItem(at: file) }
                _ = Darwin.rmdir(temporary.path)
            }
        }
        do {
            try check(item, status: .current); phase = .quiescing; try await quiesce(item.scope.runtimeOwner)
            try check(item, status: .quiesced)
            let bytes = try preferences.confirmExport(item.preferences, verifiedPrimary: item.scope.primarySessionID)
            let directory = review.archiveURL.deletingLastPathComponent().appendingPathComponent(".feral-native-archive-" + UUID().uuidString, isDirectory: true)
            try NativeProfileArchivePaths.outside(directory, config: item.scope.configRoot, data: item.scope.dataRoot)
            guard Darwin.mkdir(directory.path, 0o700) == 0 else { throw NativeProfileArchiveFailure.unavailable }
            temporary = directory; tempIdentity = try NativeProfileArchivePaths.directory(directory)
            let snapshot = directory.appendingPathComponent("native.json")
            let fd = Darwin.open(snapshot.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
            guard fd >= 0 else { throw NativeProfileArchiveFailure.unavailable }
            let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
            do { try handle.write(contentsOf: bytes); try handle.synchronize(); try handle.close() } catch { throw NativeProfileArchiveFailure.unavailable }
            let observed = try NativeProfileArchiveFile.read(snapshot, maximum: 1_048_576, capture: true); snapshotFile = observed
            guard observed.contents == bytes else { throw NativeProfileArchiveFailure.changed }
            try check(item, status: .quiesced)
            let command = NativeProfileArchiveCommand(operation: .backup, archiveURL: review.archiveURL, configRoot: item.scope.configRoot,
                dataRoot: item.scope.dataRoot, primarySessionID: item.scope.primarySessionID, snapshotURL: snapshot, snapshotSHA256: observed.sha256)
            phase = .backingUp; dispatched = true
            let raw = try await executor(command), result = try NativeProfileArchiveReceiptWire.parse(raw, command: command)
            guard ownerStatus(item.scope.runtimeOwner) == .quiesced, !Task.isCancelled,
                  try source(item.scope).0 == item.sourceRoots,
                  try source(item.scope).1 == item.primary,
                  try capture(item.scope) == bytes,
                  try NativeProfileArchiveFile.read(snapshot, maximum: 1_048_576, capture: true) == observed else { throw NativeProfileArchiveFailure.unconfirmed }
            let archive = try await Task.detached(priority: .utility) { try NativeProfileArchiveFile.read(review.archiveURL, maximum: 1_242_710_016) }.value
            guard ownerStatus(item.scope.runtimeOwner) == .quiesced, !Task.isCancelled, archive.size > 0,
                  try source(item.scope).0 == item.sourceRoots, try source(item.scope).1 == item.primary,
                  try capture(item.scope) == bytes else { throw NativeProfileArchiveFailure.unconfirmed }
            let value = NativeProfileArchiveReceipt(operation: .backup, archiveURL: review.archiveURL, archiveSHA256: archive.sha256,
                configRoot: item.scope.configRoot, dataRoot: item.scope.dataRoot, primarySessionID: item.scope.primarySessionID,
                suiteName: item.scope.suiteName, snapshotSHA256: result.snapshot, files: result.files, bytes: result.bytes, preferencesApplied: false, preferenceReadbackVerified: false)
            receipt = value; phase = .completed; return value
        } catch { throw fail(error, dispatched: dispatched) }
    }
    func confirmRestore(_ review: NativeProfileArchiveReview) async throws -> NativeProfileArchiveApplyReview {
        let item = try take(review); guard review.operation == .restore, let target = item.target, let reviewedArchive = item.archive else { throw NativeProfileArchiveFailure.invalid }
        let op = UUID(); operation = op; defer { operation = nil; preferences.cancel(item.preferences) }
        var dispatched = false
        do {
            try check(item, status: .current)
            guard try await Task.detached(priority: .utility, operation: { try NativeProfileArchiveFile.read(review.archiveURL, maximum: 1_242_710_016) }).value == reviewedArchive else { throw NativeProfileArchiveFailure.changed }
            try check(item, status: .current); phase = .quiescing; try await quiesce(item.scope.runtimeOwner)
            try check(item, status: .quiesced); let sourceSnapshot = try preferences.confirmExport(item.preferences, verifiedPrimary: item.scope.primarySessionID)
            let command = NativeProfileArchiveCommand(operation: .restore, archiveURL: review.archiveURL, configRoot: target.configRoot,
                dataRoot: target.dataRoot, primarySessionID: target.primarySessionID)
            phase = .restoring; dispatched = true
            let raw = try await executor(command), result = try NativeProfileArchiveReceiptWire.parse(raw, command: command)
            guard ownerStatus(item.scope.runtimeOwner) == .quiesced, !Task.isCancelled, let path = result.path,
                  try source(item.scope).0 == item.sourceRoots, try source(item.scope).1 == item.primary else { throw NativeProfileArchiveFailure.unconfirmed }
            guard try capture(item.scope) == sourceSnapshot else { throw NativeProfileArchiveFailure.unconfirmed }
            let snapshot = try NativeProfileArchiveFile.read(path, maximum: 1_048_576, capture: true)
            guard snapshot.sha256 == result.snapshot, let bytes = snapshot.contents else { throw NativeProfileArchiveFailure.unconfirmed }
            let actualPrimary = try NativeProfileArchiveFile.read(target.dataRoot.appendingPathComponent("primary_session_id"), maximum: 1024, capture: true)
            guard String(data: actualPrimary.contents!, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) == target.primarySessionID else { throw NativeProfileArchiveFailure.unconfirmed }
            let archive = try await Task.detached(priority: .utility) { try NativeProfileArchiveFile.read(review.archiveURL, maximum: 1_242_710_016) }.value
            guard archive == reviewedArchive, ownerStatus(item.scope.runtimeOwner) == .quiesced, !Task.isCancelled,
                  try source(item.scope).0 == item.sourceRoots, try source(item.scope).1 == item.primary,
                  try capture(item.scope) == sourceSnapshot,
                  try NativeProfileArchiveFile.read(target.dataRoot.appendingPathComponent("primary_session_id"), maximum: 1024, capture: true) == actualPrimary else { throw NativeProfileArchiveFailure.unconfirmed }
            let prefs = try preferences.prepareApply(snapshot: bytes, defaults: target.defaults, freshSuiteName: target.suiteName,
                verifiedPrimary: target.primarySessionID, restoredProfileRoot: target.configRoot, allowedDestinations: allowedDestinations)
            let next = NativeProfileArchiveApplyReview(id: UUID(), archiveURL: review.archiveURL, configRoot: target.configRoot, dataRoot: target.dataRoot,
                primarySessionID: target.primarySessionID, suiteName: target.suiteName, preferenceKeys: prefs.preferenceKeys,
                includesAvatar: prefs.includesAvatar, snapshotSHA256: snapshot.sha256, createdUptime: uptime())
            let receipt = NativeProfileArchiveReceipt(operation: .restored, archiveURL: review.archiveURL, archiveSHA256: archive.sha256,
                configRoot: target.configRoot, dataRoot: target.dataRoot, primarySessionID: target.primarySessionID, suiteName: target.suiteName,
                snapshotSHA256: snapshot.sha256, files: result.files, bytes: result.bytes, preferencesApplied: false, preferenceReadbackVerified: false)
            let roots = try [NativeProfileArchivePaths.directory(target.configRoot), NativeProfileArchivePaths.directory(target.dataRoot)]
            apply = Apply(review: next, scope: item.scope, target: target, preferences: prefs, snapshot: snapshot, archive: archive, roots: roots, primary: actualPrimary, receipt: receipt)
            applyReview = next; self.receipt = receipt; phase = .reviewingApply; return next
        } catch { throw fail(error, dispatched: dispatched) }
    }
    func confirmApply(_ review: NativeProfileArchiveApplyReview) async throws -> NativeProfileArchiveReceipt {
        guard !busy else { throw NativeProfileArchiveFailure.busy }
        guard let item = apply, item.review == review else { throw NativeProfileArchiveFailure.changed }
        apply = nil; applyReview = nil; operation = UUID(); phase = .applying
        defer { operation = nil; preferences.cancel(item.preferences) }
        var published = false
        do {
            guard ownerStatus(item.scope.runtimeOwner) == .quiesced, !Task.isCancelled,
                  uptime() >= review.createdUptime, uptime() - review.createdUptime <= 300,
                  try [NativeProfileArchivePaths.directory(item.target.configRoot), NativeProfileArchivePaths.directory(item.target.dataRoot)] == item.roots,
                  try NativeProfileArchiveFile.read(item.target.configRoot.appendingPathComponent(".feral-native-preferences.v1.json"), maximum: 1_048_576, capture: true) == item.snapshot else { throw NativeProfileArchiveFailure.changed }
            let primary = try NativeProfileArchiveFile.read(item.target.dataRoot.appendingPathComponent("primary_session_id"), maximum: 1024, capture: true)
            guard primary == item.primary,
                  try await Task.detached(priority: .utility, operation: { try NativeProfileArchiveFile.read(review.archiveURL, maximum: 1_242_710_016) }).value == item.archive,
                  ownerStatus(item.scope.runtimeOwner) == .quiesced, !Task.isCancelled else { throw NativeProfileArchiveFailure.changed }
            guard try [NativeProfileArchivePaths.directory(item.target.configRoot), NativeProfileArchivePaths.directory(item.target.dataRoot)] == item.roots,
                  try NativeProfileArchiveFile.read(item.target.configRoot.appendingPathComponent(".feral-native-preferences.v1.json"), maximum: 1_048_576, capture: true) == item.snapshot,
                  try NativeProfileArchiveFile.read(item.target.dataRoot.appendingPathComponent("primary_session_id"), maximum: 1024, capture: true) == item.primary else { throw NativeProfileArchiveFailure.changed }
            published = true
            let result = try preferences.confirmApply(item.preferences, snapshot: item.snapshot.contents!, verifiedPrimary: item.target.primarySessionID)
            guard result.readbackVerified, ownerStatus(item.scope.runtimeOwner) == .quiesced else { throw NativeProfileArchiveFailure.unconfirmed }
            let value = NativeProfileArchiveReceipt(operation: .preferencesApplied, archiveURL: item.receipt.archiveURL, archiveSHA256: item.receipt.archiveSHA256,
                configRoot: item.target.configRoot, dataRoot: item.target.dataRoot, primarySessionID: item.target.primarySessionID,
                suiteName: item.target.suiteName, snapshotSHA256: item.snapshot.sha256, files: item.receipt.files, bytes: item.receipt.bytes,
                preferencesApplied: true, preferenceReadbackVerified: true)
            receipt = value; phase = .completed; return value
        } catch { throw fail(error, dispatched: published) }
    }
}
