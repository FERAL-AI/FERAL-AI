import Foundation
import CoreFoundation
import Darwin

enum NativeRuntimePortFailure: String, LocalizedError {
    case invalidEnvironmentPort, invalidSavedPort, invalidSettings, unreadableSettings

    var errorDescription: String? {
        switch self {
        case .invalidEnvironmentPort:
            return "The explicit local runtime port must be a decimal integer from 1 to 65535. No runtime was started."
        case .invalidSavedPort:
            return "The saved network.port must be an integer or decimal string from 1 to 65535. No runtime was started."
        case .invalidSettings:
            return "The saved runtime settings are malformed or exceed 1 MiB. Repair settings.json before starting the runtime."
        case .unreadableSettings:
            return "The saved runtime settings could not be read. No runtime was started."
        }
    }
}

/// Matches config.runtime.brain_port precedence, without silently substituting
/// a different endpoint after malformed configuration or a listener conflict.
/// The caller provides a bounded read of FERAL_HOME/settings.json; nil means
/// that file is absent. This selector writes nothing and reserves no socket.
enum NativeRuntimePortFeature {
    static let defaultPort: UInt16 = 9090
    static let maximumSettingsBytes = 1_048_576

    static func resolve(environment: [String: String],
                        readSettings: () throws -> Data?) throws -> UInt16 {
        for key in ["FERAL_PORT", "FERAL_BRAIN_PORT"] {
            if let explicit = environment[key] {
                guard let port = decimalPort(explicit) else { throw NativeRuntimePortFailure.invalidEnvironmentPort }
                return port
            }
        }
        let data: Data?
        do { data = try readSettings() }
        catch let failure as NativeRuntimePortFailure { throw failure }
        catch { throw NativeRuntimePortFailure.unreadableSettings }
        guard let data else { return defaultPort }
        guard data.count <= maximumSettingsBytes,
              let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            throw NativeRuntimePortFailure.invalidSettings
        }
        guard let rawNetwork = body["network"] else { return defaultPort }
        guard let network = rawNetwork as? [String: Any] else { throw NativeRuntimePortFailure.invalidSettings }
        guard let rawPort = network["port"] else { return defaultPort }
        if let text = rawPort as? String {
            guard let port = decimalPort(text) else { throw NativeRuntimePortFailure.invalidSavedPort }
            return port
        }
        guard let number = rawPort as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(),
              ["c", "s", "i", "l", "q", "C", "S", "I", "L", "Q"].contains(String(cString: number.objCType)),
              number.doubleValue >= 1, number.doubleValue <= 65535 else {
            throw NativeRuntimePortFailure.invalidSavedPort
        }
        return number.uint16Value
    }

    /// Read only an owned regular settings file. Nonblocking, no-follow open
    /// prevents a FIFO or final-component symlink from stalling native startup.
    /// Profile ancestor validation remains the caller's responsibility.
    static func readSavedSettings(at url: URL) throws -> Data? {
        guard url.isFileURL, url.host == nil || url.host == "" else {
            throw NativeRuntimePortFailure.unreadableSettings
        }
        let descriptor = Darwin.open(url.path, O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC)
        guard descriptor >= 0 else {
            if errno == ENOENT { return nil }
            throw NativeRuntimePortFailure.unreadableSettings
        }
        defer { Darwin.close(descriptor) }
        var before = stat()
        guard fstat(descriptor, &before) == 0,
              before.st_mode & S_IFMT == S_IFREG, before.st_uid == geteuid(),
              before.st_size >= 0 else { throw NativeRuntimePortFailure.unreadableSettings }
        guard before.st_size <= maximumSettingsBytes else { throw NativeRuntimePortFailure.invalidSettings }
        var bytes = Data(), buffer = [UInt8](repeating: 0, count: 65_536)
        while bytes.count <= maximumSettingsBytes {
            let remaining = min(buffer.count, maximumSettingsBytes + 1 - bytes.count)
            let count = Darwin.read(descriptor, &buffer, remaining)
            guard count >= 0 else { throw NativeRuntimePortFailure.unreadableSettings }
            if count == 0 { break }
            bytes.append(buffer, count: count)
        }
        guard bytes.count <= maximumSettingsBytes else { throw NativeRuntimePortFailure.invalidSettings }
        var after = stat(), current = stat()
        guard fstat(descriptor, &after) == 0, lstat(url.path, &current) == 0,
              current.st_mode & S_IFMT == S_IFREG, current.st_uid == geteuid(),
              before.st_dev == after.st_dev, before.st_ino == after.st_ino,
              before.st_dev == current.st_dev, before.st_ino == current.st_ino,
              before.st_size == after.st_size, bytes.count == Int(before.st_size),
              before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec,
              before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec,
              before.st_ctimespec.tv_sec == after.st_ctimespec.tv_sec,
              before.st_ctimespec.tv_nsec == after.st_ctimespec.tv_nsec else {
            throw NativeRuntimePortFailure.unreadableSettings
        }
        return bytes
    }

    private static func decimalPort(_ raw: String) -> UInt16? {
        guard !raw.isEmpty, raw.utf8.count <= 5,
              raw.utf8.allSatisfy({ (48...57).contains($0) }),
              let number = UInt16(raw), number > 0 else { return nil }
        return number
    }
}
