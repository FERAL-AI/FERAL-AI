import Foundation

enum NativeProfileLayoutFailure: String, LocalizedError {
    case invalidEnvironment, conflictingDataRoot, unsupportedPath, uninspectablePath

    var errorDescription: String? {
        switch self {
        case .invalidEnvironment:
            return "The native profile path must be a nonempty, bounded absolute file path."
        case .conflictingDataRoot:
            return "FERAL_DATA_HOME differs from FERAL_HOME. This backend stores config and data in FERAL_HOME; no data was moved."
        case .unsupportedPath:
            return "The native profile path contains a symbolic link or a non-directory component."
        case .uninspectablePath:
            return "The native profile path could not be verified. No runtime was started."
        }
    }
}

/// A trusted caller supplies metadata only, without exposing profile contents.
enum NativeProfilePathKind {
    case missing, directory, symbolicLink, other, unavailable
}

struct NativeProfileLayout: Equatable {
    let configRoot: URL
    let dataRoot: URL
    let backendEnvironment: [String: String]
    let usesDefaultHome: Bool
}

/// Pure selection matching config.loader's FERAL_HOME precedence.
/// The backend does not consume FERAL_DATA_HOME as an independent data path.
/// This resolver creates no files, reads no contents and authorizes no migration.
/// Metadata is a point-in-time check, not a filesystem lease or archive authority.
enum NativeProfileLayoutFeature {
    static let maximumPathBytes = 4096
    static let defaultDirectoryName = ".feral-native-preview"

    static func resolve(environment: [String: String], userHome: URL,
                        inspect: (URL) throws -> NativeProfilePathKind) throws -> NativeProfileLayout {
        let usesDefault = environment["FERAL_HOME"] == nil
        let selected: String
        if let explicit = environment["FERAL_HOME"] {
            selected = try lexicalPath(explicit)
        } else {
            guard userHome.isFileURL, userHome.host == nil || userHome.host == "" else {
                throw NativeProfileLayoutFailure.invalidEnvironment
            }
            let base = try lexicalPath(userHome.path)
            selected = try lexicalPath(base + "/" + defaultDirectoryName)
        }
        if let explicitData = environment["FERAL_DATA_HOME"] {
            guard try lexicalPath(explicitData) == selected else {
                throw NativeProfileLayoutFailure.conflictingDataRoot
            }
        }
        try verifyAncestors(selected, inspect: inspect)
        // Do not use standardizedFileURL or resolvingSymlinksInPath: either can
        // adopt an alias such as /private/tmp versus /tmp without review.
        let root = URL(fileURLWithPath: selected, isDirectory: true)
        var childEnvironment = environment
        childEnvironment["FERAL_HOME"] = selected
        childEnvironment["FERAL_DATA_HOME"] = selected
        return NativeProfileLayout(configRoot: root, dataRoot: root,
                                   backendEnvironment: childEnvironment, usesDefaultHome: usesDefault)
    }

    private static func lexicalPath(_ raw: String) throws -> String {
        guard !raw.isEmpty, raw.utf8.count <= maximumPathBytes,
              raw.hasPrefix("/"), !raw.hasPrefix("//"),
              !raw.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }) else {
            throw NativeProfileLayoutFailure.invalidEnvironment
        }
        let components = raw.split(separator: "/", omittingEmptySubsequences: true)
        guard !components.contains("..") else { throw NativeProfileLayoutFailure.invalidEnvironment }
        let retained = components.filter { $0 != "." }
        guard !retained.isEmpty else { throw NativeProfileLayoutFailure.invalidEnvironment }
        return "/" + retained.joined(separator: "/")
    }

    private static func verifyAncestors(_ selected: String,
                                        inspect: (URL) throws -> NativeProfilePathKind) throws {
        let components = selected.split(separator: "/")
        var paths = ["/"]
        var current = ""
        for component in components {
            current += "/" + component
            paths.append(current)
        }
        var missingAncestor = false
        for path in paths {
            let kind: NativeProfilePathKind
            do { kind = try inspect(URL(fileURLWithPath: path, isDirectory: true)) }
            catch { throw NativeProfileLayoutFailure.uninspectablePath }
            switch kind {
            case .directory:
                guard !missingAncestor else { throw NativeProfileLayoutFailure.uninspectablePath }
            case .missing:
                guard path != "/" else { throw NativeProfileLayoutFailure.uninspectablePath }
                missingAncestor = true
            case .symbolicLink, .other: throw NativeProfileLayoutFailure.unsupportedPath
            case .unavailable: throw NativeProfileLayoutFailure.uninspectablePath
            }
        }
    }
}
