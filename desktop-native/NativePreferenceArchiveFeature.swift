import Foundation
import CryptoKit
import Darwin

enum NativePreferenceArchiveFailure: String, LocalizedError {
    case invalid, changed, occupied, primary, unavailable, unconfirmed
    var errorDescription: String? {
        switch self {
        case .invalid: return "The native preference snapshot is unsupported or malformed."
        case .changed: return "The reviewed preference snapshot or profile changed. Prepare a new review."
        case .occupied: return "Preference import requires a fresh FERAL suite. Existing preferences were not replaced."
        case .primary: return "The verified primary installation does not match this preference snapshot."
        case .unavailable: return "Native preference migration is unavailable. Original preferences were retained."
        case .unconfirmed: return "Preference publication was not confirmed. Inspect the destination before trying again; no reset was performed."
        }
    }
}

struct NativePreferenceArchiveReview: Equatable {
    enum Direction { case export, apply }
    let id: UUID
    let direction: Direction
    let primarySessionID: String
    let suiteName: String
    let profileRoot: URL
    let preferenceKeys: [String]
    let snapshotBytes: Int
    let includesAvatar: Bool
    let createdUptime: TimeInterval
}

struct NativePreferenceArchiveReceipt {
    let primarySessionID: String
    let suiteName: String
    let keysApplied: Int
    let readbackVerified: Bool
    // This component never restores OS keys, registers login items or starts tasks.
    let runtimeStarted = false
}

/// Reviewable preference snapshots, not an archive runner or second datastore.
/// UserDefaults remains canonical. The parent must verify the primary identity,
/// quiesce the app, and attach these bytes to the existing hashed profile archive.
@MainActor final class NativePreferenceArchiveFeature {
    static let maximumBytes = 1_048_576
    private static let maximumAvatarBytes = 8_388_608
    private static let fixedKeys: Set<String> = ["displayName", "avatarChoice", "onboarded",
        "desktop.keepRunningWhenClosed", "desktop.menuBarEnabled", "desktop.restoreLastDestination", "desktop.lastDestination",
        "native.savedContextModes.v1", "native.pendingSavedContextCreation.v1", "native.pendingContextRecovery.v1"]
    private struct Prepared {
        let review: NativePreferenceArchiveReview
        let defaults: UserDefaults
        let root: URL
        let destinations: Set<String>
        let bytes: Data
        let preferences: [String: Any]
        let rootIdentity: String
    }
    private var prepared: [UUID: Prepared] = [:]
    private let uptime: () -> TimeInterval
    // Trusted fixture seams only; neither is supplied by a snapshot/client.
    private let beforePublication: () throws -> Void
    private let afterPublication: () throws -> Void
    init(uptime: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
         beforePublication: @escaping () throws -> Void = {}, afterPublication: @escaping () throws -> Void = {}) {
        self.uptime = uptime; self.beforePublication = beforePublication; self.afterPublication = afterPublication
    }
    func cancel(_ review: NativePreferenceArchiveReview) { prepared[review.id] = nil }
    private static func selectionKey(_ primary: String) -> String {
        "feral.native.selectedConversation." + primary.utf8.map { String(format: "%02x", $0) }.joined()
    }
    private static func suiteValid(_ value: String) -> Bool {
        (value.hasPrefix("ai.feral.") || value.hasPrefix("feral.")) && value.utf8.count <= 128
            && value.unicodeScalars.allSatisfy { CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-").contains($0) }
    }
    private static func canonical(_ value: [String: Any]) throws -> Data {
        // Foundation sortedKeys uses numeric/system-locale ordering. Archive
        // bytes use explicit UTF-8 lexical order, shared with the backend.
        let bytes = try encodeCanonical(value)
        guard bytes.count <= maximumBytes else { throw NativePreferenceArchiveFailure.invalid }
        return bytes
    }
    private static func encodeString(_ value: String) -> Data {
        var result = "\""
        for scalar in value.unicodeScalars {
            switch scalar.value {
            case 0x22: result += "\\\""
            case 0x5c: result += "\\\\"
            case 8: result += "\\b"
            case 9: result += "\\t"
            case 10: result += "\\n"
            case 12: result += "\\f"
            case 13: result += "\\r"
            case 0...31: result += String(format: "\\u%04x", scalar.value)
            default: result.unicodeScalars.append(scalar)
            }
        }
        return Data((result + "\"").utf8)
    }
    private static func encodeCanonical(_ value: Any) throws -> Data {
        if let object = value as? [String: Any] {
            var bytes = Data("{".utf8)
            let keys = object.keys.sorted { $0.utf8.lexicographicallyPrecedes($1.utf8) }
            for (offset, key) in keys.enumerated() {
                if offset > 0 { bytes.append(44) }
                bytes.append(encodeString(key)); bytes.append(58)
                bytes.append(try encodeCanonical(object[key]!))
                guard bytes.count <= maximumBytes else { throw NativePreferenceArchiveFailure.invalid }
            }
            bytes.append(125); return bytes
        }
        if let list = value as? [Any] {
            var bytes = Data("[".utf8)
            for (offset, item) in list.enumerated() {
                if offset > 0 { bytes.append(44) }
                bytes.append(try encodeCanonical(item))
                guard bytes.count <= maximumBytes else { throw NativePreferenceArchiveFailure.invalid }
            }
            bytes.append(93); return bytes
        }
        if let string = value as? String { return encodeString(string) }
        if let boolean = NativeContextCheckpointWire.boolean(value) { return Data((boolean ? "true" : "false").utf8) }
        if let integer = NativeContextCheckpointWire.integer(value) { return Data(String(integer).utf8) }
        throw NativePreferenceArchiveFailure.invalid
    }
    private static func rootIdentity(_ root: URL) throws -> String {
        // Foundation normalizes /private/tmp to its /tmp alias on macOS.
        // Compare POSIX realpath instead, so aliases/ancestor links are refused.
        guard root.isFileURL, let canonical = realpath(root.path, nil) else { throw NativePreferenceArchiveFailure.invalid }
        defer { free(canonical) }
        guard String(cString: canonical) == root.path else { throw NativePreferenceArchiveFailure.invalid }
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: root.path), attrs[.type] as? FileAttributeType == .typeDirectory,
              let device = attrs[.systemNumber] as? NSNumber, let inode = attrs[.systemFileNumber] as? NSNumber else { throw NativePreferenceArchiveFailure.invalid }
        return "\(device):\(inode)"
    }
    private static func avatar(_ relative: String, root: URL) throws -> [String: Any] {
        let parts = relative.split(separator: "/", omittingEmptySubsequences: false)
        guard parts.count == 2, parts[0] == "avatars", !parts[1].isEmpty, parts[1] != ".", parts[1] != "..",
              relative.utf8.count <= 512, !relative.contains(":"), !relative.contains("\\"),
              !relative.unicodeScalars.contains(where: {
                  [.control, .format, .surrogate].contains($0.properties.generalCategory)
              }) else { throw NativePreferenceArchiveFailure.invalid }
        _ = try rootIdentity(root)
        _ = try rootIdentity(root.appendingPathComponent("avatars", isDirectory: true))
        let url = root.appendingPathComponent(relative)
        let fd = Darwin.open(url.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { throw NativePreferenceArchiveFailure.invalid }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        var before = stat(), after = stat()
        guard fstat(fd, &before) == 0, (before.st_mode & S_IFMT) == S_IFREG,
              before.st_size > 0, before.st_size <= maximumAvatarBytes else { throw NativePreferenceArchiveFailure.invalid }
        guard let bytes = try? handle.read(upToCount: maximumAvatarBytes + 1) else { throw NativePreferenceArchiveFailure.unavailable }
        guard bytes.count == Int(before.st_size), fstat(fd, &after) == 0,
              before.st_dev == after.st_dev, before.st_ino == after.st_ino, before.st_size == after.st_size,
              before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec, before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec,
              before.st_ctimespec.tv_sec == after.st_ctimespec.tv_sec, before.st_ctimespec.tv_nsec == after.st_ctimespec.tv_nsec else { throw NativePreferenceArchiveFailure.changed }
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: url.path), attrs[.type] as? FileAttributeType == .typeRegular,
              (attrs[.systemFileNumber] as? NSNumber)?.uint64Value == UInt64(before.st_ino) else { throw NativePreferenceArchiveFailure.changed }
        _ = try rootIdentity(root.appendingPathComponent("avatars", isDirectory: true))
        return ["relative_path": relative, "bytes": bytes.count, "sha256": SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()]
    }
    private static func validated(_ values: [String: Any], primary: String, destinations: Set<String>) throws -> [String: Any] {
        guard NativeSessionRecoveryWire.validID(primary), values.count <= fixedKeys.count + 1,
              Set(values.keys).isSubset(of: fixedKeys.union([selectionKey(primary)])) else { throw NativePreferenceArchiveFailure.invalid }
        for (key, value) in values {
            switch key {
            case "displayName": guard let name = value as? String, name.utf8.count <= 1024 else { throw NativePreferenceArchiveFailure.invalid }
            case "avatarChoice": guard let choice = value as? String, ["photo", "orb", "imported"].contains(choice) else { throw NativePreferenceArchiveFailure.invalid }
            case "onboarded", "desktop.keepRunningWhenClosed", "desktop.menuBarEnabled", "desktop.restoreLastDestination":
                guard NativeContextCheckpointWire.boolean(value) != nil else { throw NativePreferenceArchiveFailure.invalid }
            case "desktop.lastDestination": guard let destination = value as? String, destinations.contains(destination) else { throw NativePreferenceArchiveFailure.invalid }
            case "native.savedContextModes.v1":
                guard let map = value as? [String: [String]], Set(map.keys).isSubset(of: [primary]),
                      let ids = map[primary], ids.count <= 1000, Set(ids).count == ids.count,
                      ids.allSatisfy(NativeSessionRecoveryWire.validID) else { throw NativePreferenceArchiveFailure.invalid }
            case "native.pendingSavedContextCreation.v1":
                guard let map = value as? [String: String], Set(map.keys).isSubset(of: [primary]),
                      let id = map[primary], NativeSessionRecoveryWire.validID(id) else { throw NativePreferenceArchiveFailure.invalid }
            case "native.pendingContextRecovery.v1":
                guard let map = value as? [String: [String: [String: Any]]], Set(map.keys).isSubset(of: [primary]),
                      let rows = map[primary], rows.count <= 1000 else { throw NativePreferenceArchiveFailure.invalid }
                for (session, row) in rows {
                    guard NativeSessionRecoveryWire.validID(session), Set(row.keys) == ["contract_version", "session_id", "generation", "revision", "attempt_id", "acknowledge_unknown_effects"],
                          NativeContextCheckpointWire.boolean(row["acknowledge_unknown_effects"]) == true else { throw NativePreferenceArchiveFailure.invalid }
                    var fence = row; fence["state"] = "in_progress"; fence["durable"] = true; fence["requires_unknown_effects_acknowledgement"] = true
                    guard NativeContextRecoveryFence.parse(fence, sessionID: session) != nil else { throw NativePreferenceArchiveFailure.invalid }
                }
            default: guard key == selectionKey(primary), let id = value as? String, NativeSessionRecoveryWire.validID(id) else { throw NativePreferenceArchiveFailure.invalid }
            }
        }
        return values
    }
    private static func capture(_ defaults: UserDefaults, suite: String, primary: String, root: URL, destinations: Set<String>) throws -> Data {
        guard suiteValid(suite) else { throw NativePreferenceArchiveFailure.invalid }
        let domain = defaults.persistentDomain(forName: suite) ?? [:]
        var values = domain.filter { fixedKeys.contains($0.key) || $0.key == selectionKey(primary) }
        // Other installations' selections/fences do not move with this primary.
        for key in ["native.savedContextModes.v1", "native.pendingSavedContextCreation.v1", "native.pendingContextRecovery.v1"] {
            if let map = domain[key] as? [String: Any] { if let own = map[primary] { values[key] = [primary: own] } else { values[key] = nil } }
        }
        values = try validated(values, primary: primary, destinations: destinations)
        var body: [String: Any] = ["format_version": 1, "primary_session_id": primary, "preferences": values]
        if values["avatarChoice"] as? String == "imported" {
            guard let path = domain["importedAvatarPath"] as? String else { throw NativePreferenceArchiveFailure.invalid }
            let prefix = root.path + "/"
            guard path.hasPrefix(prefix) else { throw NativePreferenceArchiveFailure.invalid }
            body["avatar"] = try avatar(String(path.dropFirst(prefix.count)), root: root)
        }
        return try canonical(body)
    }
    private static func decode(_ data: Data, primary: String, root: URL, destinations: Set<String>) throws -> [String: Any] {
        guard data.count <= maximumBytes, let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              Set(body.keys).isSubset(of: ["format_version", "primary_session_id", "preferences", "avatar"]),
              NativeContextCheckpointWire.integer(body["format_version"]) == 1,
              let owner = body["primary_session_id"] as? String, NativeSessionRecoveryWire.validID(owner),
              let raw = body["preferences"] as? [String: Any], try canonical(body) == data else { throw NativePreferenceArchiveFailure.invalid }
        guard owner == primary else { throw NativePreferenceArchiveFailure.primary }
        var values = try validated(raw, primary: primary, destinations: destinations)
        if values["avatarChoice"] as? String == "imported" {
            guard let saved = body["avatar"] as? [String: Any], Set(saved.keys) == ["relative_path", "bytes", "sha256"],
                  let path = saved["relative_path"] as? String, let digest = saved["sha256"] as? String,
                  let count = NativeContextCheckpointWire.integer(saved["bytes"], minimum: 1, maximum: Int64(maximumAvatarBytes)) else { throw NativePreferenceArchiveFailure.invalid }
            let observed = try avatar(path, root: root)
            guard observed["sha256"] as? String == digest, NativeContextCheckpointWire.integer(observed["bytes"]) == count else { throw NativePreferenceArchiveFailure.changed }
            values["importedAvatarPath"] = root.appendingPathComponent(path).path
        } else if body["avatar"] != nil { throw NativePreferenceArchiveFailure.invalid }
        return values
    }
    func prepareExport(defaults: UserDefaults, suiteName: String, verifiedPrimary: String, profileRoot: URL, allowedDestinations: Set<String>) throws -> NativePreferenceArchiveReview {
        let identity = try Self.rootIdentity(profileRoot)
        let bytes = try Self.capture(defaults, suite: suiteName, primary: verifiedPrimary, root: profileRoot, destinations: allowedDestinations)
        return save(direction: .export, defaults: defaults, suite: suiteName, primary: verifiedPrimary, root: profileRoot, destinations: allowedDestinations, bytes: bytes, values: [:], identity: identity)
    }
    private func save(direction: NativePreferenceArchiveReview.Direction, defaults: UserDefaults, suite: String, primary: String, root: URL, destinations: Set<String>, bytes: Data, values: [String: Any], identity: String) -> NativePreferenceArchiveReview {
        // Bound pending review memory; replacing a review never performs work.
        prepared.removeAll()
        let body = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any]
        let keys = (body?["preferences"] as? [String: Any]).map { Array($0.keys).sorted() } ?? []
        let review = NativePreferenceArchiveReview(id: UUID(), direction: direction, primarySessionID: primary, suiteName: suite, profileRoot: root, preferenceKeys: keys, snapshotBytes: bytes.count, includesAvatar: body?["avatar"] != nil, createdUptime: uptime())
        prepared[review.id] = Prepared(review: review, defaults: defaults, root: root, destinations: destinations, bytes: bytes, preferences: values, rootIdentity: identity)
        return review
    }
    private func consume(_ review: NativePreferenceArchiveReview, primary: String) throws -> Prepared {
        guard let item = prepared.removeValue(forKey: review.id), item.review == review,
              uptime() >= review.createdUptime, uptime() - review.createdUptime <= 300 else { throw NativePreferenceArchiveFailure.changed }
        guard review.primarySessionID == primary else { throw NativePreferenceArchiveFailure.primary }
        guard try Self.rootIdentity(item.root) == item.rootIdentity else { throw NativePreferenceArchiveFailure.changed }
        return item
    }
    func confirmExport(_ review: NativePreferenceArchiveReview, verifiedPrimary: String) throws -> Data {
        let item = try consume(review, primary: verifiedPrimary)
        guard review.direction == .export,
              try Self.capture(item.defaults, suite: review.suiteName, primary: verifiedPrimary, root: item.root, destinations: item.destinations) == item.bytes else { throw NativePreferenceArchiveFailure.changed }
        return item.bytes
    }
    func prepareApply(snapshot: Data, defaults: UserDefaults, freshSuiteName: String, verifiedPrimary: String, restoredProfileRoot: URL, allowedDestinations: Set<String>) throws -> NativePreferenceArchiveReview {
        guard Self.suiteValid(freshSuiteName) else { throw NativePreferenceArchiveFailure.invalid }
        guard defaults.persistentDomain(forName: freshSuiteName) == nil else { throw NativePreferenceArchiveFailure.occupied }
        let identity = try Self.rootIdentity(restoredProfileRoot)
        let values = try Self.decode(snapshot, primary: verifiedPrimary, root: restoredProfileRoot, destinations: allowedDestinations)
        return save(direction: .apply, defaults: defaults, suite: freshSuiteName, primary: verifiedPrimary, root: restoredProfileRoot, destinations: allowedDestinations, bytes: snapshot, values: values, identity: identity)
    }
    func confirmApply(_ review: NativePreferenceArchiveReview, snapshot: Data, verifiedPrimary: String) throws -> NativePreferenceArchiveReceipt {
        let item = try consume(review, primary: verifiedPrimary)
        guard review.direction == .apply, snapshot == item.bytes else { throw NativePreferenceArchiveFailure.changed }
        guard item.defaults.persistentDomain(forName: review.suiteName) == nil else { throw NativePreferenceArchiveFailure.occupied }
        let current = try Self.decode(snapshot, primary: verifiedPrimary, root: item.root, destinations: item.destinations)
        guard NSDictionary(dictionary: current).isEqual(to: item.preferences) else { throw NativePreferenceArchiveFailure.changed }
        do { try beforePublication() } catch { throw NativePreferenceArchiveFailure.unavailable }
        // Recheck after the trusted pre-publication seam. Never merge or reset.
        guard item.defaults.persistentDomain(forName: review.suiteName) == nil else { throw NativePreferenceArchiveFailure.occupied }
        guard try Self.rootIdentity(item.root) == item.rootIdentity,
              NSDictionary(dictionary: try Self.decode(snapshot, primary: verifiedPrimary, root: item.root, destinations: item.destinations)).isEqual(to: current) else { throw NativePreferenceArchiveFailure.changed }
        item.defaults.setPersistentDomain(current, forName: review.suiteName)
        do { try afterPublication() } catch { throw NativePreferenceArchiveFailure.unconfirmed }
        guard item.defaults.synchronize(), let observed = item.defaults.persistentDomain(forName: review.suiteName),
              NSDictionary(dictionary: observed).isEqual(to: current) else { throw NativePreferenceArchiveFailure.unconfirmed }
        return NativePreferenceArchiveReceipt(primarySessionID: verifiedPrimary, suiteName: review.suiteName, keysApplied: current.count, readbackVerified: true)
    }
}
