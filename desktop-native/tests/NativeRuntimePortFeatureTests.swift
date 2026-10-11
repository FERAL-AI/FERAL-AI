import Foundation
import Darwin

@main struct NativeRuntimePortFeatureTests {
    static var count = 0

    static func check(_ value: Bool, _ label: String) {
        count += 1
        precondition(value, label)
    }
    static func refused(_ expected: NativeRuntimePortFailure,
                        _ operation: () throws -> UInt16) {
        do { _ = try operation(); preconditionFailure("Expected typed refusal") }
        catch let failure as NativeRuntimePortFailure {
            check(failure == expected, "Exact typed refusal")
            check(!failure.localizedDescription.contains("private-sentinel"), "No private settings or underlying error in refusal")
        } catch { preconditionFailure("Unexpected error type") }
    }
    static func resolve(_ json: String?, environment: [String: String] = [:]) throws -> UInt16 {
        try NativeRuntimePortFeature.resolve(environment: environment) { json.map { Data($0.utf8) } }
    }
    static func fileFixtures() throws {
        let root = URL(fileURLWithPath: "/private/tmp/feral-runtime-port-fixture-" + UUID().uuidString, isDirectory: true)
        let manager = FileManager.default
        try manager.createDirectory(at: root, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        defer { try? manager.removeItem(at: root) }
        func selected(_ name: String, environment: [String: String] = [:]) throws -> UInt16 {
            try NativeRuntimePortFeature.resolve(environment: environment) {
                try NativeRuntimePortFeature.readSavedSettings(at: root.appendingPathComponent(name))
            }
        }
        check(try selected("absent.json") == 9090, "Absent disposable settings retain default")
        let regular = root.appendingPathComponent("settings.json")
        let original = Data("{\"network\":{\"port\":9431}}".utf8)
        try original.write(to: regular)
        for _ in 0..<3 {
            check(try selected("settings.json") == 9431, "Production file reader preserves saved endpoint")
        }
        check(try Data(contentsOf: regular) == original, "Settings selection leaves fixture contents unchanged")
        try Data(repeating: 32, count: NativeRuntimePortFeature.maximumSettingsBytes + 1)
            .write(to: root.appendingPathComponent("large.json"))
        refused(.invalidSettings) { try selected("large.json") }
        let prefix = "{\"padding\":\"", suffix = "\"}"
        let exact = prefix + String(repeating: "a", count: NativeRuntimePortFeature.maximumSettingsBytes - prefix.utf8.count - suffix.utf8.count) + suffix
        try Data(exact.utf8).write(to: root.appendingPathComponent("limit.json"))
        check(try selected("limit.json") == 9090, "Production reader accepts exact size bound")
        try manager.createDirectory(at: root.appendingPathComponent("directory.json"), withIntermediateDirectories: false)
        refused(.unreadableSettings) { try selected("directory.json") }
        try manager.createSymbolicLink(at: root.appendingPathComponent("link.json"), withDestinationURL: regular)
        refused(.unreadableSettings) { try selected("link.json") }
        try manager.createSymbolicLink(at: root.appendingPathComponent("dangling.json"), withDestinationURL: root.appendingPathComponent("absent.json"))
        refused(.unreadableSettings) { try selected("dangling.json") }
        let fifo = root.appendingPathComponent("fifo.json")
        guard mkfifo(fifo.path, 0o600) == 0 else { preconditionFailure("Could not create disposable FIFO") }
        let started = ProcessInfo.processInfo.systemUptime
        refused(.unreadableSettings) { try selected("fifo.json") }
        check(ProcessInfo.processInfo.systemUptime - started < 2, "Unconnected FIFO refuses promptly without a writer")
        check(try selected("fifo.json", environment: ["FERAL_PORT": "9432"]) == 9432,
              "Explicit port skips even unsupported saved settings")
        try Data("private-sentinel".utf8).write(to: root.appendingPathComponent("malformed.json"))
        refused(.invalidSettings) { try selected("malformed.json") }
    }
    static func main() throws {
        check(try resolve(nil) == 9090, "Absent settings use canonical stable default")
        for json in ["{}", "{\"network\":{}}", "{\"llm\":{\"model\":\"private-sentinel\"}}"] {
            check(try resolve(json) == 9090, "Missing port uses default, unrelated settings preserved")
        }
        let settings = "{\"network\":{\"port\":9321,\"bind_host\":\"0.0.0.0\"},\"access\":{\"pairing_mode\":\"local\"}}"
        for _ in 0..<10 {
            check(try resolve(settings) == 9321, "Repeated startup selection retains saved pairing endpoint")
            check(try resolve(nil) == 9090, "Repeated startup without settings retains canonical endpoint")
        }
        for (literal, expected) in [("1", 1), ("65535", 65535), ("\"9470\"", 9470), ("\"09090\"", 9090)] {
            check(try resolve("{\"network\":{\"port\":\(literal)}}") == UInt16(expected), "Bounded saved integer and decimal-string ports accepted")
        }
        var reads = 0
        let selected = try NativeRuntimePortFeature.resolve(environment: ["FERAL_PORT": "9401", "FERAL_BRAIN_PORT": "9402"]) {
            reads += 1; throw NSError(domain: "private-sentinel", code: 1)
        }
        check(selected == 9401 && reads == 0, "Primary explicit port takes precedence without reading settings")
        check(try resolve(settings, environment: ["FERAL_BRAIN_PORT": "9402"]) == 9402, "Existing backend port alias takes precedence over saved port")
        check(try resolve("broken private-sentinel", environment: ["FERAL_PORT": "9401"]) == 9401, "Unused settings do not override a strict explicit port")
        for key in ["FERAL_PORT", "FERAL_BRAIN_PORT"] {
            for value in ["", " ", "9090 ", " 9090", "+9090", "-1", "0", "65536", "9999999999999999999999", "9090.0", "9e3", "true", "９０９０", "private-sentinel", "9090\n"] {
                refused(.invalidEnvironmentPort) { try resolve(settings, environment: [key: value]) }
            }
        }
        refused(.invalidEnvironmentPort) {
            try resolve(settings, environment: ["FERAL_PORT": "private-sentinel", "FERAL_BRAIN_PORT": "9402"])
        }
        for literal in ["true", "false", "9090.0", "9.09e3", "9090.5", "-1", "0", "65536", "18446744073709551615", "null", "[]", "{}", "\"\"", "\"0\"", "\"65536\"", "\" 9090\"", "\"+9090\"", "\"9090.0\"", "\"private-sentinel\"", "\"９０９０\""] {
            refused(.invalidSavedPort) { try resolve("{\"network\":{\"port\":\(literal)}}") }
        }
        for json in ["", "private-sentinel", "{", "[]", "null", "true", "{\"network\":null}", "{\"network\":[]}", "{\"network\":\"private-sentinel\"}"] {
            refused(.invalidSettings) { try resolve(json) }
        }
        refused(.unreadableSettings) {
            try NativeRuntimePortFeature.resolve(environment: [:]) {
                throw NSError(domain: "private-sentinel", code: 1, userInfo: [NSLocalizedDescriptionKey: "private-sentinel profile contents"])
            }
        }
        let prefix = "{\"padding\":\"", suffix = "\"}"
        let maximum = NativeRuntimePortFeature.maximumSettingsBytes
        let atLimit = prefix + String(repeating: "a", count: maximum - prefix.utf8.count - suffix.utf8.count) + suffix
        check(try resolve(atLimit) == 9090, "Valid settings at read bound are accepted")
        refused(.invalidSettings) { try resolve(atLimit + " ") }
        try fileFixtures()
        print("NativeRuntimePortFeatureTests: \(count) assertions passed; isolated settings/file fixtures, no personal profiles, listeners or runtime launches.")
    }
}
