import Foundation
import CoreFoundation
import Darwin

enum NativeSavedProfileKind: String { case personal, preview }
enum NativeProfileSource: String {
    case environment, savedPersonal, savedPreview, previewDefault
    var label: String {
        switch self {
        case .environment: return "FERAL_HOME environment override"
        case .savedPersonal: return "Saved existing FERAL profile"
        case .savedPreview: return "Saved native preview profile"
        case .previewDefault: return "Unchanged native preview default"
        }
    }
}

enum NativeProfileSelectionFailure: String, LocalizedError {
    case invalidStoredChoice, unavailable, environmentOverride, busy, changed, expired, unconfirmed
    var errorDescription: String? {
        switch self {
        case .invalidStoredChoice: return "The saved native profile choice is unsupported. Startup is paused; no other profile was selected."
        case .unavailable: return "The native profile choice could not be reviewed. Existing profile data was not changed."
        case .environmentOverride: return "FERAL_HOME controls this launch. Remove that explicit override before reviewing a saved choice."
        case .busy: return "Finish or reconcile current chat, voice, coding and archive work before reviewing another profile."
        case .changed: return "The profile choice, target directory or review changed. Prepare a new review."
        case .expired: return "The profile review expired. Prepare a new review."
        case .unconfirmed: return "Saving the next-launch choice was not confirmed. Quit and check the actual saved choice before trying again; no profile was started."
        }
    }
}

enum NativeSavedProfileChoice {
    static let key = "native.profileSelection.v1"
    static func decode(_ raw: Any?) throws -> NativeSavedProfileKind? {
        guard let raw else { return nil }
        guard let body = raw as? [String: Any], Set(body.keys) == ["schema_version", "kind"],
              let number = body["schema_version"] as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(),
              ["c", "s", "i", "l", "q", "C", "S", "I", "L", "Q"].contains(String(cString: number.objCType)),
              number.compare(NSNumber(value: 1)) == .orderedSame,
              let text = body["kind"] as? String,
              let kind = NativeSavedProfileKind(rawValue: text) else {
            throw NativeProfileSelectionFailure.invalidStoredChoice
        }
        return kind
    }
    static func encode(_ kind: NativeSavedProfileKind) -> [String: Any] {
        ["schema_version": 1, "kind": kind.rawValue]
    }
}

struct NativeProfileDirectorySnapshot: Equatable {
    let exists: Bool
    let device: Int64
    let inode: UInt64
    let owner: UInt32
}

/// Metadata only: no directory creation, credential/config reads or process access.
enum NativeProfileSelectionMetadata {
    static func kind(_ url: URL) -> NativeProfilePathKind {
        var row = stat()
        guard lstat(url.path, &row) == 0 else { return errno == ENOENT ? .missing : .unavailable }
        switch row.st_mode & S_IFMT {
        case S_IFDIR: return .directory
        case S_IFLNK: return .symbolicLink
        default: return .other
        }
    }
    static func snapshot(_ url: URL) throws -> NativeProfileDirectorySnapshot {
        var row = stat()
        guard lstat(url.path, &row) == 0 else {
            if errno == ENOENT { return .init(exists: false, device: 0, inode: 0, owner: 0) }
            throw NativeProfileSelectionFailure.unavailable
        }
        guard row.st_mode & S_IFMT == S_IFDIR, row.st_uid == geteuid() else {
            throw NativeProfileSelectionFailure.unavailable
        }
        return .init(exists: true, device: Int64(row.st_dev), inode: UInt64(row.st_ino), owner: row.st_uid)
    }
    static func ownedDirectory(_ url: URL) throws -> Bool { try snapshot(url).exists }
    static func resolve(environment: [String: String], userHome: URL, storedChoice: Any?) -> Result<NativeProfileLayout, NativeProfileLayoutFailure> {
        do {
            return .success(try NativeProfileLayoutFeature.resolve(environment: environment, userHome: userHome,
                storedChoice: storedChoice, inspect: kind, ownedDirectory: ownedDirectory))
        } catch let failure as NativeProfileLayoutFailure { return .failure(failure) }
        catch { return .failure(.uninspectablePath) }
    }
}

struct NativeProfileSelectionReview: Equatable, Identifiable {
    let id: UUID
    let domain: String
    let oldChoice: NativeSavedProfileKind?
    let newChoice: NativeSavedProfileKind
    let activeRoot: URL?
    let targetRoot: URL
    let targetSnapshot: NativeProfileDirectorySnapshot
    let createdUptime: TimeInterval
    static let disclosure = "Save which existing profile this native app will use on its next normal launch. The current runtime and its data root do not switch. Quit and reopen explicitly; normal Quit may interrupt existing background work. No files, credentials, models, memory, pairing or conversations are copied, deleted, migrated or reset. Stored credentials still require the existing explicit unlock review. Stop other profile writers before activation; this preference is not a writer lease or permission to replay unknown tasks."
}

/// Stores only a versioned choice in the native app's existing preference domain.
/// One review is retained; confirmation never starts/stops a runtime or edits a profile.
final class NativeProfileSelectionController {
    typealias Read = () throws -> Any?
    typealias Write = ([String: Any]) throws -> Void
    let environment: [String: String]
    let userHome: URL
    let domain: String
    let activeRoot: URL?
    private let read: Read
    private let write: Write
    private let inspect: (URL) throws -> NativeProfilePathKind
    private let snapshot: (URL) throws -> NativeProfileDirectorySnapshot
    private let uptime: () -> TimeInterval
    private var review: NativeProfileSelectionReview?
    init(environment: [String: String], userHome: URL, domain: String, activeRoot: URL?,
         read: @escaping Read, write: @escaping Write,
         inspect: @escaping (URL) throws -> NativeProfilePathKind = NativeProfileSelectionMetadata.kind,
         snapshot: @escaping (URL) throws -> NativeProfileDirectorySnapshot = NativeProfileSelectionMetadata.snapshot,
         uptime: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }) {
        self.environment = environment; self.userHome = userHome; self.domain = domain; self.activeRoot = activeRoot
        self.read = read; self.write = write; self.inspect = inspect; self.snapshot = snapshot; self.uptime = uptime
    }
    func prepare(_ kind: NativeSavedProfileKind, idle: Bool) throws -> NativeProfileSelectionReview {
        guard idle else { throw NativeProfileSelectionFailure.busy }
        guard environment["FERAL_HOME"] == nil else { throw NativeProfileSelectionFailure.environmentOverride }
        guard !domain.isEmpty, domain.utf8.count <= 256,
              !domain.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else {
            throw NativeProfileSelectionFailure.unavailable
        }
        let old = try NativeSavedProfileChoice.decode(read())
        let layout = try NativeProfileLayoutFeature.resolve(environment: environment, userHome: userHome,
            storedChoice: NativeSavedProfileChoice.encode(kind), inspect: inspect,
            ownedDirectory: { try self.snapshot($0).exists })
        let identity = try snapshot(layout.configRoot)
        let now = uptime()
        guard now.isFinite, now >= 0 else { throw NativeProfileSelectionFailure.unavailable }
        let value = NativeProfileSelectionReview(id: UUID(), domain: domain, oldChoice: old,
            newChoice: kind, activeRoot: activeRoot, targetRoot: layout.configRoot,
            targetSnapshot: identity, createdUptime: now)
        review = value; return value
    }
    func cancel(_ supplied: NativeProfileSelectionReview) {
        if review == supplied { review = nil }
    }
    func confirm(_ supplied: NativeProfileSelectionReview, idle: Bool) throws {
        guard idle else { throw NativeProfileSelectionFailure.busy }
        guard review == supplied, supplied.domain == domain, supplied.activeRoot == activeRoot,
              environment["FERAL_HOME"] == nil else { throw NativeProfileSelectionFailure.changed }
        let elapsed = uptime() - supplied.createdUptime
        guard elapsed.isFinite, elapsed >= 0, elapsed <= 300 else {
            review = nil; throw NativeProfileSelectionFailure.expired
        }
        guard try NativeSavedProfileChoice.decode(read()) == supplied.oldChoice else {
            review = nil; throw NativeProfileSelectionFailure.changed
        }
        let layout = try NativeProfileLayoutFeature.resolve(environment: environment, userHome: userHome,
            storedChoice: NativeSavedProfileChoice.encode(supplied.newChoice), inspect: inspect,
            ownedDirectory: { try self.snapshot($0).exists })
        guard layout.configRoot == supplied.targetRoot, try snapshot(layout.configRoot) == supplied.targetSnapshot else {
            review = nil; throw NativeProfileSelectionFailure.changed
        }
        // Consume before a possibly uncertain write; this review can never replay it.
        review = nil
        do {
            try write(NativeSavedProfileChoice.encode(supplied.newChoice))
            guard try NativeSavedProfileChoice.decode(read()) == supplied.newChoice else {
                throw NativeProfileSelectionFailure.unconfirmed
            }
        } catch { throw NativeProfileSelectionFailure.unconfirmed }
    }
}
