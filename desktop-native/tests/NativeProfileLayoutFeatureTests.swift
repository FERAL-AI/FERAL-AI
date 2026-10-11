import Foundation

@main struct NativeProfileLayoutFeatureTests {
    static var count = 0
    static let home = URL(fileURLWithPath: "/private/tmp/synthetic-native-user", isDirectory: true)
    static let root = "/private/tmp/synthetic-native-profile"

    static func check(_ value: @autoclosure () -> Bool, _ label: String) {
        count += 1
        precondition(value(), label)
    }
    static func refused(_ expected: NativeProfileLayoutFailure,
                        _ operation: () throws -> NativeProfileLayout) {
        do { _ = try operation(); preconditionFailure("Expected typed refusal") }
        catch let failure as NativeProfileLayoutFailure {
            check(failure == expected, "Exact typed failure")
            check(!(failure.errorDescription ?? "").contains("private-sentinel"), "Failure diagnostics are redacted")
        } catch { preconditionFailure("Unexpected error type") }
    }
    static func resolve(_ environment: [String: String],
                        kinds: [String: NativeProfilePathKind] = [:]) throws -> NativeProfileLayout {
        try NativeProfileLayoutFeature.resolve(environment: environment, userHome: home,
                                               inspect: { kinds[$0.path] ?? .directory })
    }

    static func main() throws {
        let defaults = try resolve(["XDG_CONFIG_HOME": "/ignored-config", "XDG_DATA_HOME": "/ignored-data",
                                    "UNRELATED": "preserved"])
        let expected = home.path + "/.feral-native-preview"
        check(defaults.configRoot.path == expected && defaults.dataRoot.path == expected,
              "Native default remains unchanged and both backend roots are the actual FERAL_HOME")
        check(defaults.usesDefaultHome, "Default selection is explicit")
        check(defaults.backendEnvironment["FERAL_HOME"] == expected,
              "Injected native home overrides backend XDG fallbacks")
        check(defaults.backendEnvironment["FERAL_DATA_HOME"] == expected,
              "No misleading home/data injection")
        check(defaults.backendEnvironment["UNRELATED"] == "preserved", "Unrelated environment retained")

        let original = ["FERAL_HOME": root, "FERAL_DATA_HOME": root, "UNRELATED": "preserved"]
        let explicit = try resolve(original)
        check(!explicit.usesDefaultHome && explicit.configRoot.path == root, "Explicit home is retained")
        check(explicit.dataRoot == explicit.configRoot, "Actual backend same-root precedence")
        check(original["FERAL_HOME"] == root, "Input environment not mutated")
        let noData = try resolve(["FERAL_HOME": root])
        check(noData.backendEnvironment["FERAL_DATA_HOME"] == root, "Absent data override reports actual backend root")

        for spelling in [root + "/", root + "//", "/private//tmp/./synthetic-native-profile/."] {
            let equivalent = try resolve(["FERAL_HOME": spelling, "FERAL_DATA_HOME": root + "/./"])
            check(equivalent.configRoot.path == root && equivalent.dataRoot.path == root,
                  "Only lexical slash and dot normalization")
        }
        check(explicit.configRoot.path.hasPrefix("/private/tmp/"), "No Foundation /private/tmp alias rewriting")
        let unicode = try resolve(["FERAL_HOME": "/private/tmp/companion café", "FERAL_DATA_HOME": "/private/tmp/companion café/"])
        check(unicode.configRoot.path == "/private/tmp/companion café", "Unicode and interior spaces preserved")
        let trailingSpace = try resolve(["FERAL_HOME": "/private/tmp/profile "])
        check(trailingSpace.configRoot.path == "/private/tmp/profile ", "Valid filename spaces never silently trimmed")

        var inspected: [String] = []
        let missing = try NativeProfileLayoutFeature.resolve(environment: ["FERAL_HOME": root + "/new/nested"], userHome: home,
            inspect: { url in
                inspected.append(url.path)
                return url.path.hasPrefix(root) ? .missing : .directory
            })
        check(missing.configRoot.path == root + "/new/nested", "Missing selected root can be created later by runtime")
        check(inspected == ["/", "/private", "/private/tmp", root, root + "/new", root + "/new/nested"],
              "Every lexical ancestor verified, no filesystem contents read")

        var calls = 0
        refused(.conflictingDataRoot) {
            try NativeProfileLayoutFeature.resolve(environment: ["FERAL_HOME": root, "FERAL_DATA_HOME": root + "/data"], userHome: home,
                inspect: { _ in calls += 1; return .directory })
        }
        check(calls == 0, "Conflicting data binding refused before metadata access or runtime creation")
        refused(.conflictingDataRoot) { try resolve(["FERAL_DATA_HOME": root]) }

        for value in ["", " ", "relative", "~/profile", "file:///private/tmp/profile", "/", "/./",
                      "//server/profile", root + "/../other", root + "/private-sentinel\n", root + "\0",
                      "/" + String(repeating: "a", count: 4096)] {
            refused(.invalidEnvironment) { try resolve(["FERAL_HOME": value]) }
        }
        for value in ["", "relative", root + "/../synthetic-native-profile", root + "\t"] {
            refused(.invalidEnvironment) { try resolve(["FERAL_HOME": root, "FERAL_DATA_HOME": value]) }
        }
        let remote = URL(string: "file://private-sentinel.example/profile")!
        refused(.invalidEnvironment) {
            try NativeProfileLayoutFeature.resolve(environment: [:], userHome: remote, inspect: { _ in .directory })
        }
        refused(.invalidEnvironment) {
            try NativeProfileLayoutFeature.resolve(environment: [:], userHome: URL(string: "https://private-sentinel.example/profile")!,
                                                   inspect: { _ in .directory })
        }
        for path in ["/private", "/private/tmp", root] {
            refused(.unsupportedPath) { try resolve(["FERAL_HOME": root], kinds: [path: .symbolicLink]) }
            refused(.unsupportedPath) { try resolve(["FERAL_HOME": root], kinds: [path: .other]) }
            refused(.uninspectablePath) { try resolve(["FERAL_HOME": root], kinds: [path: .unavailable]) }
        }
        refused(.uninspectablePath) { try resolve(["FERAL_HOME": root], kinds: ["/": .missing]) }
        refused(.uninspectablePath) { try resolve(["FERAL_HOME": root], kinds: ["/private": .missing]) }
        enum PrivateMetadataError: Error { case privateSentinel }
        refused(.uninspectablePath) {
            try NativeProfileLayoutFeature.resolve(environment: ["FERAL_HOME": root], userHome: home,
                                                   inspect: { _ in throw PrivateMetadataError.privateSentinel })
        }
        refused(.uninspectablePath) {
            try NativeProfileLayoutFeature.resolve(environment: ["FERAL_HOME": root], userHome: home,
                inspect: { _ in throw NSError(domain: "private-sentinel", code: 1, userInfo: [NSLocalizedDescriptionKey: "private-sentinel profile details"]) })
        }
        var visits = 0
        refused(.unsupportedPath) {
            try NativeProfileLayoutFeature.resolve(environment: ["FERAL_HOME": root], userHome: home,
                inspect: { url in visits += 1; return url.path == "/private" ? .symbolicLink : .directory })
        }
        check(visits == 2, "Stops at unsafe ancestor without examining selected profile")
        print("NativeProfileLayoutFeature: \(count) assertions passed; pure metadata fixtures, no profile reads or migration.")
    }
}
