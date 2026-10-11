import Foundation

private struct FixtureFailure: Error { }
@main struct NativeProfileSelectionFeatureTests {
    static var count = 0
    static let home = URL(fileURLWithPath: "/private/tmp/synthetic-profile-selection-user", isDirectory: true)
    static let preview = home.appendingPathComponent(".feral-native-preview", isDirectory: true)
    static let personal = home.appendingPathComponent(".feral", isDirectory: true)
    static func check(_ value: Bool, _ message: String) {
        count += 1; precondition(value, message)
    }
    static func refuses(_ expected: NativeProfileSelectionFailure, _ block: () throws -> Void) {
        do { try block(); preconditionFailure("Expected selection refusal") }
        catch let error as NativeProfileSelectionFailure { check(error == expected, "Wrong typed selection refusal") }
        catch { preconditionFailure("Unexpected error") }
    }
    static func layout(_ env: [String: String] = [:], _ choice: Any? = nil,
                       owned: Bool = true, missing: Bool = false) throws -> NativeProfileLayout {
        try NativeProfileLayoutFeature.resolve(environment: env, userHome: home, storedChoice: choice,
            inspect: { missing && $0 == personal ? .missing : .directory }, ownedDirectory: { _ in owned })
    }
    static func main() throws {
        let original = try layout()
        check(original.configRoot == preview && original.dataRoot == preview && original.source == .previewDefault && original.usesDefaultHome, "Absent choice changed default")
        for kind in [NativeSavedProfileKind.personal, .preview] {
            let raw = NativeSavedProfileChoice.encode(kind)
            check(try NativeSavedProfileChoice.decode(raw) == kind, "Versioned choice round trip")
            let selected = try layout(["UNRELATED": "kept"], raw)
            let target = kind == .personal ? personal : preview
            check(selected.configRoot == target && selected.dataRoot == target, "Stored choice split roots")
            check(selected.backendEnvironment["FERAL_HOME"] == target.path && selected.backendEnvironment["FERAL_DATA_HOME"] == target.path, "Child overrides split roots")
            check(selected.backendEnvironment["UNRELATED"] == "kept", "Unrelated environment changed")
            check(selected.avatarDirectory == target.appendingPathComponent("avatars", isDirectory: true), "Avatar root mismatch")
            check(selected.recoveryJournalURL == target.appendingPathComponent("native-context-recovery.json"), "Recovery root mismatch")
            check(!selected.usesDefaultHome, "Explicit stored choice reported default")
        }
        let override = try layout(["FERAL_HOME": "/private/tmp/environment-profile"], ["broken": true])
        check(override.configRoot.path == "/private/tmp/environment-profile" && override.source == .environment, "Explicit environment failed to override malformed inactive choice")
        for invalid: Any in ["personal", true, ["kind": "personal"], ["schema_version": true, "kind": "personal"],
                            ["schema_version": 1.5, "kind": "personal"], ["schema_version": 2, "kind": "personal"],
                            ["schema_version": 1, "kind": "other"], ["schema_version": 1, "kind": "personal", "path": "/private-sentinel"]] {
            refuses(.invalidStoredChoice) { _ = try NativeSavedProfileChoice.decode(invalid) }
            do { _ = try layout([:], invalid); preconditionFailure("Invalid choice fell back") }
            catch let error as NativeProfileLayoutFailure { check(error == .invalidStoredChoice, "Invalid choice wrong layout refusal") }
        }
        for (owned, missing) in [(false, false), (true, true)] {
            do { _ = try layout([:], NativeSavedProfileChoice.encode(.personal), owned: owned, missing: missing); preconditionFailure("Unowned/missing personal accepted") }
            catch let error as NativeProfileLayoutFailure { check(error == .personalProfileUnavailable, "Missing/unowned personal wrong refusal") }
        }
        do { _ = try layout(["FERAL_DATA_HOME": preview.path], NativeSavedProfileChoice.encode(.personal)); preconditionFailure("Conflicting data root accepted") }
        catch let error as NativeProfileLayoutFailure { check(error == .conflictingDataRoot, "Wrong conflicting-root error") }
        let saved = try layout([:], NativeSavedProfileChoice.encode(.personal))
        do {
            try NativeProfileLayoutFeature.validateForUse(saved, inspect: { $0 == personal ? .symbolicLink : .directory }, ownedDirectory: { _ in true })
            preconditionFailure("Replaced profile symlink accepted")
        } catch let error as NativeProfileLayoutFailure { check(error == .unsupportedPath, "Revalidation wrong refusal") }

        var value: Any? = nil; var writes = 0; var clock: TimeInterval = 10
        var identity = NativeProfileDirectorySnapshot(exists: true, device: 1, inode: 2, owner: 42)
        var failWrite = false; var corruptReadback = false
        let controller = NativeProfileSelectionController(environment: [:], userHome: home, domain: "fixture.domain", activeRoot: preview,
            read: { value }, write: { body in writes += 1; if failWrite { throw FixtureFailure() }; value = corruptReadback ? ["schema_version": 1, "kind": "preview"] : body },
            inspect: { _ in .directory }, snapshot: { _ in identity }, uptime: { clock })
        let cancel = try controller.prepare(.personal, idle: true)
        check(writes == 0 && value == nil, "Review wrote preferences")
        check(cancel.activeRoot == preview && cancel.targetRoot == personal, "Review target scope wrong")
        controller.cancel(cancel)
        refuses(.changed) { try controller.confirm(cancel, idle: true) }
        check(writes == 0, "Cancelled review wrote")
        refuses(.busy) { _ = try controller.prepare(.personal, idle: false) }
        let expired = try controller.prepare(.personal, idle: true); clock = 311
        refuses(.expired) { try controller.confirm(expired, idle: true) }; check(writes == 0, "Expired review wrote")
        clock = 20
        let stale = try controller.prepare(.personal, idle: true)
        value = NativeSavedProfileChoice.encode(.preview)
        refuses(.changed) { try controller.confirm(stale, idle: true) }; check(writes == 0, "Changed preference wrote")
        value = nil
        let replaced = try controller.prepare(.personal, idle: true)
        identity = .init(exists: true, device: 1, inode: 3, owner: 42)
        refuses(.changed) { try controller.confirm(replaced, idle: true) }; check(writes == 0, "Changed directory wrote")
        let good = try controller.prepare(.personal, idle: true)
        refuses(.busy) { try controller.confirm(good, idle: false) }; check(writes == 0, "Busy confirmation wrote")
        try controller.confirm(good, idle: true)
        let readback = try NativeSavedProfileChoice.decode(value)
        check(writes == 1 && readback == .personal, "Exact confirmation missing readback")
        check(controller.activeRoot == preview, "Confirmation hot swapped active root")
        refuses(.changed) { try controller.confirm(good, idle: true) }; check(writes == 1, "Used review replayed")
        let failure = try controller.prepare(.preview, idle: true); failWrite = true
        refuses(.unconfirmed) { try controller.confirm(failure, idle: true) }
        refuses(.changed) { try controller.confirm(failure, idle: true) }; check(writes == 2, "Uncertain write replayed")
        failWrite = false; value = nil; corruptReadback = true
        let mismatched = try controller.prepare(.personal, idle: true)
        refuses(.unconfirmed) { try controller.confirm(mismatched, idle: true) }
        refuses(.changed) { try controller.confirm(mismatched, idle: true) }; check(writes == 3, "Wrong readback replayed")
        let locked = NativeProfileSelectionController(environment: ["FERAL_HOME": preview.path], userHome: home, domain: "fixture", activeRoot: preview,
            read: { nil }, write: { _ in preconditionFailure("Environment override wrote") }, inspect: { _ in .directory }, snapshot: { _ in identity })
        refuses(.environmentOverride) { _ = try locked.prepare(.personal, idle: true) }
        check(!NativeProfileSelectionReview.disclosure.contains("private-sentinel"), "Disclosure leaked private data")
        print("NativeProfileSelectionFeature: \(count) assertions passed; inert preferences/metadata only, no profile/runtime/keychain effects")
    }
}
