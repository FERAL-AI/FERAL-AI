import Foundation

@main struct NativePreferenceArchiveFeatureTests {
    @MainActor static var count = 0
    @MainActor static func check(_ value: @autoclosure () -> Bool, _ label: String) {
        count += 1; precondition(value(), label)
    }
    @MainActor static func refused(_ expected: NativePreferenceArchiveFailure? = nil, _ operation: () throws -> Void) {
        do { try operation(); preconditionFailure("Expected a typed refusal") }
        catch let error as NativePreferenceArchiveFailure { check(expected == nil || error == expected, "Expected typed failure: \(error.rawValue)") }
        catch { preconditionFailure("Unexpected non-redacted error type") }
    }
    static func canonical(_ body: [String: Any]) throws -> Data {
        try JSONSerialization.data(withJSONObject: body, options: [.sortedKeys, .withoutEscapingSlashes])
    }
    @MainActor static func main() async throws {
        let fm = FileManager.default
        let root = URL(fileURLWithPath: "/private/tmp/feral-native-preference-archive-" + UUID().uuidString, isDirectory: true)
        try fm.createDirectory(at: root, withIntermediateDirectories: false)
        defer { try? fm.removeItem(at: root) }
        let sourceRoot = root.appendingPathComponent("source", isDirectory: true)
        let targetRoot = root.appendingPathComponent("restored", isDirectory: true)
        for directory in [sourceRoot, targetRoot] {
            try fm.createDirectory(at: directory.appendingPathComponent("avatars", isDirectory: true), withIntermediateDirectories: true)
            try Data("disposable-avatar-bytes".utf8).write(to: directory.appendingPathComponent("avatars/pet.png"))
        }
        let sourceSuite = "ai.feral.preference-fixture.source." + UUID().uuidString
        let source = UserDefaults(suiteName: sourceSuite)!
        var domains = [sourceSuite]
        defer { for domain in domains { source.removePersistentDomain(forName: domain) } }
        func fresh() -> (UserDefaults, String) {
            let name = "ai.feral.preference-fixture.target." + UUID().uuidString
            domains.append(name); return (UserDefaults(suiteName: name)!, name)
        }
        let primary = "fixture-primary", selected = "fixture-saved-chat"
        let selectionKey = "feral.native.selectedConversation." + primary.utf8.map { String(format: "%02x", $0) }.joined()
        let destinations: Set<String> = ["Chat", "Memory", "Settings"]
        let fence = NativeContextRecoveryFence(sessionID: selected, generation: UUID().uuidString.lowercased(), revision: 4, attemptID: UUID().uuidString.lowercased())
        let original: [String: Any] = ["displayName": "Fixture companion", "avatarChoice": "imported", "onboarded": true,
            "importedAvatarPath": sourceRoot.appendingPathComponent("avatars/pet.png").path,
            "desktop.menuBarEnabled": true, "desktop.keepRunningWhenClosed": false,
            "desktop.restoreLastDestination": true, "desktop.lastDestination": "Memory", selectionKey: selected,
            "native.savedContextModes.v1": [primary: [selected], "foreign-primary": ["foreign-chat"]],
            "native.pendingSavedContextCreation.v1": [primary: "fixture-new-chat"],
            "native.pendingContextRecovery.v1": [primary: [selected: fence.parameters]],
            "api_key": "fixture-secret-excluded", "loginRegistered": true]
        source.setPersistentDomain(original, forName: sourceSuite)
        let feature = NativePreferenceArchiveFeature()
        let export = try feature.prepareExport(defaults: source, suiteName: sourceSuite, verifiedPrimary: primary, profileRoot: sourceRoot, allowedDestinations: destinations)
        check(export.direction == .export && export.includesAvatar, "prepare describes scope without publication")
        check(export.profileRoot == sourceRoot && !export.preferenceKeys.contains("api_key"), "review exposes exact profile root and allowlisted scope")
        let snapshot = try feature.confirmExport(export, verifiedPrimary: primary)
        check(!String(decoding: snapshot, as: UTF8.self).contains("fixture-secret-excluded"), "credentials excluded")
        check(!String(decoding: snapshot, as: UTF8.self).contains(sourceRoot.path), "absolute source path not portable payload")
        check(!String(decoding: snapshot, as: UTF8.self).contains("foreign-primary"), "other primary metadata excluded")
        check(NSDictionary(dictionary: source.persistentDomain(forName: sourceSuite)!).isEqual(to: original), "export leaves original intact")
        refused(.changed) { _ = try feature.confirmExport(export, verifiedPrimary: primary) }
        let (target, targetSuite) = fresh()
        let apply = try feature.prepareApply(snapshot: snapshot, defaults: target, freshSuiteName: targetSuite, verifiedPrimary: primary, restoredProfileRoot: targetRoot, allowedDestinations: destinations)
        check(target.persistentDomain(forName: targetSuite) == nil, "prepare does not import")
        let receipt = try feature.confirmApply(apply, snapshot: snapshot, verifiedPrimary: primary)
        check(receipt.readbackVerified && !receipt.runtimeStarted, "real readback without task execution")
        check(target.string(forKey: "displayName") == "Fixture companion" && target.bool(forKey: "onboarded"), "canonical profile keys restored")
        check(target.string(forKey: "importedAvatarPath") == targetRoot.appendingPathComponent("avatars/pet.png").path, "portable avatar mapped to restored root")
        check(target.string(forKey: "desktop.lastDestination") == "Memory" && target.object(forKey: "desktop.keepRunningWhenClosed") as? Bool == false, "desktop reader keys retained")
        check(target.object(forKey: "api_key") == nil && target.object(forKey: "loginRegistered") == nil, "credentials and login effects not imported")
        let context = NativeContextCheckpointPreferences(defaults: target)
        check(context.required(selected, primary: primary) && !context.required(selected, primary: "foreign-primary"), "actual context-mode reader preserves primary guard")
        check(context.pending(primary: primary) == "fixture-new-chat", "actual pending-context reader retained")
        let retainedFence = try context.pendingRecovery(session: selected, primary: primary)
        check(retainedFence == fence, "actual legacy recovery reader retains fence without execution")
        let recovery = NativeSessionRecoveryModel(preferences: target, transport: { request in
            let data = try canonical(["session_id": primary])
            return (data, HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        })
        recovery.configure(baseURL: URL(string: "http://127.0.0.1:9999"), connectionID: UUID())
        _ = try await recovery.resolvePrimary()
        let retainedSelection = try recovery.selectedConversationID()
        check(retainedSelection == selected, "actual primary-scoped selection reader retains selected chat")
        refused(.occupied) { _ = try feature.prepareApply(snapshot: snapshot, defaults: target, freshSuiteName: targetSuite, verifiedPrimary: primary, restoredProfileRoot: targetRoot, allowedDestinations: destinations) }
        check(UserDefaults(suiteName: targetSuite)!.string(forKey: "displayName") == "Fixture companion", "fresh reader sees persisted imported preferences")

        let (empty, emptySuite) = fresh()
        refused(.primary) { _ = try feature.prepareApply(snapshot: snapshot, defaults: empty, freshSuiteName: emptySuite, verifiedPrimary: "foreign-primary", restoredProfileRoot: targetRoot, allowedDestinations: destinations) }
        let body = try JSONSerialization.jsonObject(with: snapshot) as! [String: Any]
        for mutation in ["future", "unknown", "badBool", "foreignKey", "foreignMap", "unsafeAvatar", "oversized", "notJSON", "duplicate"] {
            var changed = body
            var prefs = changed["preferences"] as! [String: Any]
            var data: Data
            switch mutation {
            case "future": changed["format_version"] = 2
            case "unknown": prefs["api_key"] = "fixture-private-sentinel"
            case "badBool": prefs["onboarded"] = "true"
            case "foreignKey": prefs["feral.native.selectedConversation.foreign"] = selected
            case "foreignMap": prefs["native.savedContextModes.v1"] = ["foreign-primary": [selected]]
            case "unsafeAvatar": var avatar = changed["avatar"] as! [String: Any]; avatar["relative_path"] = "avatars/../../private"; changed["avatar"] = avatar
            default: break
            }
            changed["preferences"] = prefs
            data = try canonical(changed)
            if mutation == "oversized" { data = Data(repeating: 32, count: NativePreferenceArchiveFeature.maximumBytes + 1) }
            if mutation == "notJSON" { data = Data("invalid fixture-private-sentinel".utf8) }
            if mutation == "duplicate" { data = Data(String(decoding: snapshot, as: UTF8.self).replacingOccurrences(of: "\"format_version\":1", with: "\"format_version\":1,\"format_version\":1").utf8) }
            refused { _ = try feature.prepareApply(snapshot: data, defaults: empty, freshSuiteName: emptySuite, verifiedPrimary: primary, restoredProfileRoot: targetRoot, allowedDestinations: destinations) }
            check(empty.persistentDomain(forName: emptySuite) == nil, "invalid snapshot never populates target")
        }
        let drift = try feature.prepareExport(defaults: source, suiteName: sourceSuite, verifiedPrimary: primary, profileRoot: sourceRoot, allowedDestinations: destinations)
        source.set("Changed fixture", forKey: "displayName")
        refused(.changed) { _ = try feature.confirmExport(drift, verifiedPrimary: primary) }
        source.setPersistentDomain(original, forName: sourceSuite)
        let cancelled = try feature.prepareApply(snapshot: snapshot, defaults: empty, freshSuiteName: emptySuite, verifiedPrimary: primary, restoredProfileRoot: targetRoot, allowedDestinations: destinations)
        feature.cancel(cancelled)
        refused(.changed) { _ = try feature.confirmApply(cancelled, snapshot: snapshot, verifiedPrimary: primary) }
        let occupied = try feature.prepareApply(snapshot: snapshot, defaults: empty, freshSuiteName: emptySuite, verifiedPrimary: primary, restoredProfileRoot: targetRoot, allowedDestinations: destinations)
        empty.set("external fixture", forKey: "unrelated")
        refused(.occupied) { _ = try feature.confirmApply(occupied, snapshot: snapshot, verifiedPrimary: primary) }
        check(empty.string(forKey: "unrelated") == "external fixture", "external destination publication retained")
        let (changedData, changedDataSuite) = fresh()
        let changedReview = try feature.prepareApply(snapshot: snapshot, defaults: changedData, freshSuiteName: changedDataSuite, verifiedPrimary: primary, restoredProfileRoot: targetRoot, allowedDestinations: destinations)
        refused(.changed) { _ = try feature.confirmApply(changedReview, snapshot: snapshot + Data(" ".utf8), verifiedPrimary: primary) }
        check(changedData.persistentDomain(forName: changedDataSuite) == nil, "reviewed bytes cannot change")
        let changedAvatar = try feature.prepareApply(snapshot: snapshot, defaults: changedData, freshSuiteName: changedDataSuite, verifiedPrimary: primary, restoredProfileRoot: targetRoot, allowedDestinations: destinations)
        try Data("changed image".utf8).write(to: targetRoot.appendingPathComponent("avatars/pet.png"))
        refused(.changed) { _ = try feature.confirmApply(changedAvatar, snapshot: snapshot, verifiedPrimary: primary) }
        try Data("disposable-avatar-bytes".utf8).write(to: targetRoot.appendingPathComponent("avatars/pet.png"))
        var now: TimeInterval = 10
        let expiry = NativePreferenceArchiveFeature(uptime: { now })
        let expired = try expiry.prepareApply(snapshot: snapshot, defaults: changedData, freshSuiteName: changedDataSuite, verifiedPrimary: primary, restoredProfileRoot: targetRoot, allowedDestinations: destinations)
        now = 311
        refused(.changed) { _ = try expiry.confirmApply(expired, snapshot: snapshot, verifiedPrimary: primary) }
        let early = NativePreferenceArchiveFeature(beforePublication: { throw NativePreferenceArchiveFailure.unavailable })
        let interrupted = try early.prepareApply(snapshot: snapshot, defaults: changedData, freshSuiteName: changedDataSuite, verifiedPrimary: primary, restoredProfileRoot: targetRoot, allowedDestinations: destinations)
        refused(.unavailable) { _ = try early.confirmApply(interrupted, snapshot: snapshot, verifiedPrimary: primary) }
        check(changedData.persistentDomain(forName: changedDataSuite) == nil, "interrupted before publication preserves fresh target")
        for mutation in ["avatar", "root", "occupied"] {
            let (probe, probeSuite) = fresh()
            let moved = root.appendingPathComponent("moved-restored", isDirectory: true)
            let guarded = NativePreferenceArchiveFeature(beforePublication: {
                if mutation == "avatar" {
                    try Data("changed during review publication".utf8).write(to: targetRoot.appendingPathComponent("avatars/pet.png"))
                } else if mutation == "root" {
                    try fm.moveItem(at: targetRoot, to: moved)
                    try fm.createDirectory(at: targetRoot.appendingPathComponent("avatars", isDirectory: true), withIntermediateDirectories: true)
                    try Data("disposable-avatar-bytes".utf8).write(to: targetRoot.appendingPathComponent("avatars/pet.png"))
                } else { probe.set("concurrent fixture", forKey: "external") }
            })
            let review = try guarded.prepareApply(snapshot: snapshot, defaults: probe, freshSuiteName: probeSuite, verifiedPrimary: primary, restoredProfileRoot: targetRoot, allowedDestinations: destinations)
            refused(mutation == "occupied" ? .occupied : .changed) { _ = try guarded.confirmApply(review, snapshot: snapshot, verifiedPrimary: primary) }
            check(probe.string(forKey: "displayName") == nil, "seam drift cannot publish reviewed profile")
            if mutation == "avatar" { try Data("disposable-avatar-bytes".utf8).write(to: targetRoot.appendingPathComponent("avatars/pet.png")) }
            if mutation == "root" { try fm.removeItem(at: targetRoot); try fm.moveItem(at: moved, to: targetRoot) }
            if mutation == "occupied" { check(probe.string(forKey: "external") == "concurrent fixture", "concurrent publication retained") }
        }
        let alias = root.appendingPathComponent("alias", isDirectory: true)
        try fm.createSymbolicLink(at: alias, withDestinationURL: root)
        let (aliasTarget, aliasSuite) = fresh()
        refused(.invalid) { _ = try feature.prepareApply(snapshot: snapshot, defaults: aliasTarget, freshSuiteName: aliasSuite, verifiedPrimary: primary, restoredProfileRoot: alias.appendingPathComponent("restored", isDirectory: true), allowedDestinations: destinations) }
        let image = targetRoot.appendingPathComponent("avatars/pet.png")
        let movedImage = targetRoot.appendingPathComponent("avatars/original.png")
        try fm.moveItem(at: image, to: movedImage); try fm.createSymbolicLink(at: image, withDestinationURL: movedImage)
        refused(.invalid) { _ = try feature.prepareApply(snapshot: snapshot, defaults: aliasTarget, freshSuiteName: aliasSuite, verifiedPrimary: primary, restoredProfileRoot: targetRoot, allowedDestinations: destinations) }
        try fm.removeItem(at: image); try fm.moveItem(at: movedImage, to: image)
        let avatars = targetRoot.appendingPathComponent("avatars", isDirectory: true)
        let movedAvatars = targetRoot.appendingPathComponent("original-avatars", isDirectory: true)
        try fm.moveItem(at: avatars, to: movedAvatars); try fm.createSymbolicLink(at: avatars, withDestinationURL: movedAvatars)
        refused(.invalid) { _ = try feature.prepareApply(snapshot: snapshot, defaults: aliasTarget, freshSuiteName: aliasSuite, verifiedPrimary: primary, restoredProfileRoot: targetRoot, allowedDestinations: destinations) }
        try fm.removeItem(at: avatars); try fm.moveItem(at: movedAvatars, to: avatars)
        let primaryDrift = try feature.prepareApply(snapshot: snapshot, defaults: aliasTarget, freshSuiteName: aliasSuite, verifiedPrimary: primary, restoredProfileRoot: targetRoot, allowedDestinations: destinations)
        refused(.primary) { _ = try feature.confirmApply(primaryDrift, snapshot: snapshot, verifiedPrimary: "foreign-primary") }
        check(aliasTarget.persistentDomain(forName: aliasSuite) == nil, "primary drift never imports preferences")
        let late = NativePreferenceArchiveFeature(afterPublication: { throw NativePreferenceArchiveFailure.unavailable })
        let uncertain = try late.prepareApply(snapshot: snapshot, defaults: changedData, freshSuiteName: changedDataSuite, verifiedPrimary: primary, restoredProfileRoot: targetRoot, allowedDestinations: destinations)
        refused(.unconfirmed) { _ = try late.confirmApply(uncertain, snapshot: snapshot, verifiedPrimary: primary) }
        check(changedData.string(forKey: "displayName") == "Fixture companion", "post-publication interruption remains inspectable; no reset")
        refused(.occupied) { _ = try late.prepareApply(snapshot: snapshot, defaults: changedData, freshSuiteName: changedDataSuite, verifiedPrimary: primary, restoredProfileRoot: targetRoot, allowedDestinations: destinations) }
        check(NSDictionary(dictionary: source.persistentDomain(forName: sourceSuite)!).isEqual(to: original), "all refusal/interruption paths preserve original domain")
        let trickyName = "Féral 👓 / slash\\back\n\t\u{0001}\u{2028}\u{2029}"
        source.set(trickyName, forKey: "displayName")
        // Fences remain bound to their actual session, rather than copying an
        // existing session's reviewed recovery identity into a different row.
        var records: [String: [String: Any]] = [:]
        for session in ["thread-2", "thread-10"] {
            records[session] = NativeContextRecoveryFence(sessionID: session, generation: UUID().uuidString.lowercased(), revision: 3, attemptID: UUID().uuidString.lowercased()).parameters
        }
        source.set([primary: records], forKey: "native.pendingContextRecovery.v1")
        let lexicalReview = try feature.prepareExport(defaults: source, suiteName: sourceSuite, verifiedPrimary: primary, profileRoot: sourceRoot, allowedDestinations: destinations)
        let lexical = try feature.confirmExport(lexicalReview, verifiedPrimary: primary)
        let text = String(decoding: lexical, as: UTF8.self)
        check(text.range(of: "\"thread-10\"")!.lowerBound < text.range(of: "\"thread-2\"")!.lowerBound, "explicit UTF-8 ordering avoids Foundation numeric sorting")
        check(text.contains("\\n\\t\\u0001") && text.contains("\u{2028}\u{2029}") && text.contains("/ slash\\\\back"), "control, Unicode separators, slash and backslash use portable compact escapes")
        let (lexicalTarget, lexicalSuite) = fresh()
        let lexicalApply = try feature.prepareApply(snapshot: lexical, defaults: lexicalTarget, freshSuiteName: lexicalSuite, verifiedPrimary: primary, restoredProfileRoot: targetRoot, allowedDestinations: destinations)
        _ = try feature.confirmApply(lexicalApply, snapshot: lexical, verifiedPrimary: primary)
        check(lexicalTarget.string(forKey: "displayName") == trickyName, "portable serializer retains original Unicode/scalar content")
        source.setPersistentDomain(original, forName: sourceSuite)
        for filename in ["colon:pet.png", "back\\slash.png", "control\npet.png", "format\u{200d}pet.png"] {
            let path = sourceRoot.appendingPathComponent("avatars/" + filename)
            try Data("disposable restricted filename".utf8).write(to: path)
            source.set(path.path, forKey: "importedAvatarPath")
            refused(.invalid) { _ = try feature.prepareExport(defaults: source, suiteName: sourceSuite, verifiedPrimary: primary, profileRoot: sourceRoot, allowedDestinations: destinations) }
            try fm.removeItem(at: path)
        }
        source.setPersistentDomain(original, forName: sourceSuite)
        print("Native preference archive: \(count) assertions passed")
    }
}
