import Foundation
import CryptoKit
import Darwin

@main struct NativeProfileArchiveFeatureTests {
    @MainActor static var assertions = 0
    @MainActor static func check(_ value: @autoclosure () throws -> Bool, _ description: String) {
        assertions += 1; precondition(try! value(), description)
    }
    @MainActor static func refused(_ expected: NativeProfileArchiveFailure? = nil, _ operation: () async throws -> Void) async {
        do { try await operation(); preconditionFailure("Expected archive refusal") }
        catch let error as NativeProfileArchiveFailure { check(expected == nil || error == expected, "typed/redacted refusal: \(error)") }
        catch { preconditionFailure("Unexpected error type: \(type(of: error))") }
    }
    @MainActor final class Fixture {
        let base: URL, config: URL, data: URL, defaults: UserDefaults, suite: String
        var owner = NativeRuntimeOwnership(generation: UUID(), instanceID: UUID().uuidString,
            baseURL: URL(string: "http://127.0.0.1:9999")!, processIdentity: UUID())
        var status: NativeProfileArchiveOwnerStatus = .current
        var quiesces = 0, calls = 0, suites: [String]
        let primary = "fixture-native-profile-primary"
        init(root: URL, layout: String = "same") throws {
            base = root.appendingPathComponent(UUID().uuidString, isDirectory: true)
            config = base.appendingPathComponent("source", isDirectory: true)
            data = layout == "same" ? config : layout == "nested" ? config.appendingPathComponent("data", isDirectory: true) : base.appendingPathComponent("source-data", isDirectory: true)
            suite = "ai.feral.profile-archive.source." + UUID().uuidString
            defaults = UserDefaults(suiteName: suite)!; suites = [suite]
            try FileManager.default.createDirectory(at: config, withIntermediateDirectories: true)
            try FileManager.default.createDirectory(at: data, withIntermediateDirectories: true)
            try Data(primary.utf8).write(to: data.appendingPathComponent("primary_session_id"))
            try Data("{\"llm\":{\"max_tokens\":111}}".utf8).write(to: config.appendingPathComponent("settings.json"))
            try Data("synthetic-runtime-bytes".utf8).write(to: data.appendingPathComponent("runtime.bin"))
            defaults.setPersistentDomain(["displayName": "Féral 👓", "avatarChoice": "orb", "onboarded": true,
                "desktop.menuBarEnabled": true, "native.savedContextModes.v1": [primary: ["saved-chat"]]], forName: suite)
        }
        var scope: NativeProfileArchiveScope { NativeProfileArchiveScope(configRoot: config, dataRoot: data,
            defaults: defaults, suiteName: suite, primarySessionID: primary, runtimeOwner: owner) }
        func restart() { owner = NativeRuntimeOwnership(generation: UUID(), instanceID: UUID().uuidString,
            baseURL: URL(string: "http://127.0.0.1:9999")!, processIdentity: UUID()); status = .current }
        func target(layout: String = "same") -> NativeProfileArchiveTarget {
            let config = base.appendingPathComponent("restored-" + UUID().uuidString, isDirectory: true)
            let data = layout == "same" ? config : layout == "nested" ? config.appendingPathComponent("data", isDirectory: true) : base.appendingPathComponent("restored-data-" + UUID().uuidString, isDirectory: true)
            let suite = "ai.feral.profile-archive.target." + UUID().uuidString; suites.append(suite)
            return NativeProfileArchiveTarget(configRoot: config, dataRoot: data,
                defaults: UserDefaults(suiteName: suite)!, suiteName: suite, primarySessionID: primary)
        }
        func controller(executor: @escaping NativeProfileArchiveFeature.Executor,
                        preferences: NativePreferenceArchiveFeature? = nil,
                        quiesce: ((NativeRuntimeOwnership) async throws -> Void)? = nil,
                        uptime: @escaping () -> Double = { ProcessInfo.processInfo.systemUptime }) -> NativeProfileArchiveFeature {
            NativeProfileArchiveFeature(preferences: preferences, allowedDestinations: ["Chat", "Settings"],
                ownerStatus: { [self] captured in captured == owner ? status : .superseded },
                quiesce: quiesce ?? { [self] captured in
                    guard captured == owner, status == .current else { throw NativeProfileArchiveFailure.changed }
                    quiesces += 1; status = .quiesced
                }, executor: { [self] command in calls += 1; return try await executor(command) }, uptime: uptime)
        }
        func cleanup() { for suite in suites { defaults.removePersistentDomain(forName: suite) } }
    }
    @MainActor static func backup(_ fixture: Fixture, _ feature: NativeProfileArchiveFeature, name: String = "profile.zip") async throws -> URL {
        let archive = fixture.base.appendingPathComponent(name)
        let review = try feature.prepareBackup(scope: fixture.scope, archiveURL: archive, otherWritersStopped: true)
        check(!feature.busy && feature.phase == .reviewingBackup && fixture.quiesces == 0, "review performs no stop/archive")
        let receipt = try await feature.confirmBackup(review)
        check(receipt.operation == .backup && !receipt.preferencesApplied && !receipt.runtimeStarted, "backup acknowledged without activation")
        check(receipt.archiveSHA256.count == 64 && receipt.snapshotSHA256.count == 64, "archive and snapshot actual digest readback")
        check(try !FileManager.default.contentsOfDirectory(atPath: fixture.base.path).contains(where: { $0.hasPrefix(".feral-native-archive-") }), "owned snapshot staging removed after confirmed archive")
        return archive
    }
    @MainActor static func main() async throws {
        let fm = FileManager.default
        let root = URL(fileURLWithPath: "/private/tmp/feral-native-profile-archive-" + UUID().uuidString, isDirectory: true)
        try fm.createDirectory(at: root, withIntermediateDirectories: false)
        defer { try? fm.removeItem(at: root) }
        let core = URL(fileURLWithPath: fm.currentDirectoryPath).deletingLastPathComponent().appendingPathComponent("feral-core", isDirectory: true)
        let python = core.deletingLastPathComponent().appendingPathComponent(".venv/bin/python")
        let process = NativeProfileArchiveProcessExecutor(pythonURL: python, coreURL: core)
        let execute: NativeProfileArchiveFeature.Executor = { command in
            if let snapshot = command.snapshotURL {
                let file = try fm.attributesOfItem(atPath: snapshot.path)
                let directory = try fm.attributesOfItem(atPath: snapshot.deletingLastPathComponent().path)
                check((file[.posixPermissions] as? NSNumber)?.intValue == 0o600, "plaintext allowlisted snapshot has exclusive owner permissions")
                check((directory[.posixPermissions] as? NSNumber)?.intValue == 0o700, "snapshot staging directory private")
                check(!snapshot.path.hasPrefix(command.configRoot.path + "/") && !snapshot.path.hasPrefix(command.dataRoot.path + "/"), "snapshot explicitly outside both profile roots")
            }
            return try await process.execute(command)
        }
        var fixtures: [Fixture] = []
        defer { fixtures.forEach { $0.cleanup() } }
        func fixture(_ layout: String = "same") throws -> Fixture { let value = try Fixture(root: root, layout: layout); fixtures.append(value); return value }

        // Genuine standalone CLI, actual canonical defaults/context readers, and
        // separate explicit archive and preference confirmations for every layout.
        for layout in ["same", "nested", "split"] {
            let f = try fixture(layout), feature = f.controller(executor: execute)
            let original = f.defaults.persistentDomain(forName: f.suite)!
            let archive = try await backup(f, feature)
            check(f.calls == 1 && f.quiesces == 1, "one actual CLI invocation after exact stop")
            f.restart()
            let target = f.target(layout: layout)
            let review = try await feature.prepareRestore(scope: f.scope, archiveURL: archive, target: target, otherWritersStopped: true)
            check(review.archiveSHA256?.count == 64 && f.calls == 1, "restore review hashes input without mutation")
            let apply = try await feature.confirmRestore(review)
            check(f.calls == 2 && f.defaults.persistentDomain(forName: target.suiteName) == nil, "archive restore does not apply preferences")
            check(feature.phase == .reviewingApply && feature.receipt?.preferencesApplied == false, "second explicit apply review")
            check(apply.nativeLayoutCompatible == (layout == "same"), "explicit offline layouts do not imply native activation support")
            check(try String(contentsOf: target.dataRoot.appendingPathComponent("primary_session_id")) == f.primary, "actual restored data-root primary")
            let result = try await feature.confirmApply(apply)
            check(result.preferencesApplied && result.preferenceReadbackVerified && !result.runtimeStarted, "real fresh suite readback without start")
            check(target.defaults.string(forKey: "displayName") == "Féral 👓", "actual preference reader after apply")
            check(NativeContextCheckpointPreferences(defaults: target.defaults).required("saved-chat", primary: f.primary), "actual saved-context mode reader after apply")
            check(NSDictionary(dictionary: f.defaults.persistentDomain(forName: f.suite)!).isEqual(to: original), "original source defaults retained")
            await refused(.changed) { _ = try await feature.confirmApply(apply) }
        }

        for mutation in ["owner", "primary", "preferences", "root", "archiveOccupied", "expired"] {
            let f = try fixture(); var clock = 100.0
            let feature = f.controller(executor: execute, uptime: { clock })
            let archive = f.base.appendingPathComponent("refused.zip")
            let review = try feature.prepareBackup(scope: f.scope, archiveURL: archive, otherWritersStopped: true)
            switch mutation {
            case "owner": f.restart()
            case "primary": try Data("foreign-primary".utf8).write(to: f.data.appendingPathComponent("primary_session_id"))
            case "preferences": f.defaults.set("Changed source", forKey: "displayName")
            case "root":
                try fm.moveItem(at: f.config, to: f.base.appendingPathComponent("original"))
                try fm.createDirectory(at: f.config, withIntermediateDirectories: false)
                try Data(f.primary.utf8).write(to: f.config.appendingPathComponent("primary_session_id"))
            case "archiveOccupied": try Data("preserve existing".utf8).write(to: archive)
            default: clock += 301
            }
            await refused { _ = try await feature.confirmBackup(review) }
            check(f.calls == 0, "pre-dispatch changes never invoke archive")
            if mutation == "archiveOccupied" { check(try String(contentsOf: archive) == "preserve existing", "existing archive retained") }
            else { check(!fm.fileExists(atPath: archive.path), "no archive on refused review") }
        }
        let attestation = try fixture(), attestedFeature = attestation.controller(executor: execute)
        await refused(.otherWriters) { _ = try attestedFeature.prepareBackup(scope: attestation.scope, archiveURL: attestation.base.appendingPathComponent("no.zip"), otherWritersStopped: false) }
        let cancel = try attestedFeature.prepareBackup(scope: attestation.scope, archiveURL: attestation.base.appendingPathComponent("cancel.zip"), otherWritersStopped: true)
        try attestedFeature.cancelReview()
        await refused(.changed) { _ = try await attestedFeature.confirmBackup(cancel) }
        check(attestation.calls == 0 && attestation.quiesces == 0, "cancelled review has no side effect")

        // Exact owner supersession or source mutation during the stop await.
        for mutation in ["owner", "preferences"] {
            let f = try fixture()
            let feature = f.controller(executor: execute, quiesce: { _ in
                if mutation == "owner" { f.restart() } else { f.status = .quiesced; f.defaults.set("Changed during stop", forKey: "displayName") }
            })
            let review = try feature.prepareBackup(scope: f.scope, archiveURL: f.base.appendingPathComponent("stop.zip"), otherWritersStopped: true)
            await refused { _ = try await feature.confirmBackup(review) }
            check(f.calls == 0, "superseded stop or changed preferences cannot dispatch")
        }
        let noProof = try fixture()
        let noProofFeature = noProof.controller(executor: execute, quiesce: { _ in })
        let noProofReview = try noProofFeature.prepareBackup(scope: noProof.scope, archiveURL: noProof.base.appendingPathComponent("unproven-stop.zip"), otherWritersStopped: true)
        await refused(.changed) { _ = try await noProofFeature.confirmBackup(noProofReview) }
        check(noProof.calls == 0, "callback completion alone is not exact-owner stop proof")
        let busy = try fixture(); var resume: CheckedContinuation<Void, Never>?
        let busyFeature = busy.controller(executor: execute, quiesce: { _ in
            await withCheckedContinuation { resume = $0 }; busy.status = .quiesced
        })
        let busyReview = try busyFeature.prepareBackup(scope: busy.scope, archiveURL: busy.base.appendingPathComponent("busy.zip"), otherWritersStopped: true)
        let active = Task { try await busyFeature.confirmBackup(busyReview) }
        while resume == nil { await Task.yield() }
        await refused(.busy) { _ = try busyFeature.prepareBackup(scope: busy.scope, archiveURL: busy.base.appendingPathComponent("second.zip"), otherWritersStopped: true) }
        await refused(.busy) { try busyFeature.cancelReview() }
        resume?.resume(); _ = try await active.value

        // Genuine archive effects followed by bad/no receipt must remain
        // inspectable and block replay instead of reporting success or deleting.
        for mutation in ["duplicate", "bool", "foreignPrimary", "extra", "owner", "preferences", "throw"] {
            let f = try fixture()
            let feature = f.controller(executor: { command in
                let bytes = try await execute(command)
                if mutation == "owner" { f.restart(); return bytes }
                if mutation == "preferences" { f.defaults.set("Changed after dispatch", forKey: "displayName"); return bytes }
                if mutation == "throw" { throw NativeProfileArchiveFailure.unavailable }
                if mutation == "duplicate" { return Data(String(decoding: bytes, as: UTF8.self).replacingOccurrences(of: "{", with: "{\"status\":\"completed\",", options: [], range: nil).utf8) }
                var object = try JSONSerialization.jsonObject(with: bytes) as! [String: Any]
                if mutation == "bool" { object["preferences_applied"] = 0 }
                if mutation == "foreignPrimary" { object["native_primary_session_id"] = "foreign-primary" }
                if mutation == "extra" { object["ignored"] = true }
                return try JSONSerialization.data(withJSONObject: object)
            })
            let archive = f.base.appendingPathComponent("uncertain.zip")
            let review = try feature.prepareBackup(scope: f.scope, archiveURL: archive, otherWritersStopped: true)
            await refused(.unconfirmed) { _ = try await feature.confirmBackup(review) }
            check(fm.fileExists(atPath: archive.path) && feature.phase == .unconfirmed, "uncertain archive retained for inspection")
            await refused(.unconfirmed) { _ = try feature.prepareBackup(scope: f.scope, archiveURL: f.base.appendingPathComponent("replay.zip"), otherWritersStopped: true) }
            check(f.calls == 1, "unknown result never replays")
        }

        for mutation in ["snapshot", "primary", "root", "occupied", "owner", "archive"] {
            let f = try fixture(), feature = f.controller(executor: execute)
            let archive = try await backup(f, feature); f.restart()
            let target = f.target()
            let review = try await feature.prepareRestore(scope: f.scope, archiveURL: archive, target: target, otherWritersStopped: true)
            let apply = try await feature.confirmRestore(review)
            switch mutation {
            case "snapshot": try Data("changed snapshot".utf8).write(to: target.configRoot.appendingPathComponent(".feral-native-preferences.v1.json"))
            case "primary": try Data("foreign-primary".utf8).write(to: target.dataRoot.appendingPathComponent("primary_session_id"))
            case "root": try fm.moveItem(at: target.configRoot, to: f.base.appendingPathComponent("kept-restored-root")); try fm.createDirectory(at: target.configRoot, withIntermediateDirectories: false)
            case "occupied": target.defaults.setPersistentDomain(["preserve": true], forName: target.suiteName)
            case "owner": f.restart()
            default: try Data("changed archive".utf8).write(to: archive)
            }
            await refused { _ = try await feature.confirmApply(apply) }
            check(target.defaults.string(forKey: "displayName") == nil, "changed apply review never publishes profile")
            if mutation == "occupied" { check(target.defaults.bool(forKey: "preserve"), "occupied target retained") }
            check(f.calls == 2 && fm.fileExists(atPath: target.configRoot.path), "no restore replay or target cleanup")
        }

        for mutation in ["archive", "occupied", "source", "owner"] {
            let f = try fixture(), feature = f.controller(executor: execute)
            let archive = try await backup(f, feature); f.restart()
            let target = f.target()
            let review = try await feature.prepareRestore(scope: f.scope, archiveURL: archive, target: target, otherWritersStopped: true)
            if mutation == "archive" { try Data("changed after first restore review".utf8).write(to: archive) }
            if mutation == "occupied" { target.defaults.setPersistentDomain(["preserve": true], forName: target.suiteName) }
            if mutation == "source" { f.defaults.set("Changed source", forKey: "displayName") }
            if mutation == "owner" { f.restart() }
            await refused { _ = try await feature.confirmRestore(review) }
            check(f.calls == 1 && !fm.fileExists(atPath: target.configRoot.path), "changed restore review cannot dispatch or create roots")
        }
        let cancelledReview = try fixture(), cancelledReviewFeature = cancelledReview.controller(executor: execute)
        let cancelledReviewArchive = try await backup(cancelledReview, cancelledReviewFeature); cancelledReview.restart()
        let cancelledReviewTarget = cancelledReview.target()
        let cancelledReviewPrepared = try await cancelledReviewFeature.prepareRestore(scope: cancelledReview.scope, archiveURL: cancelledReviewArchive, target: cancelledReviewTarget, otherWritersStopped: true)
        let cancelledApply = try await cancelledReviewFeature.confirmRestore(cancelledReviewPrepared)
        try cancelledReviewFeature.cancelReview()
        await refused(.changed) { _ = try await cancelledReviewFeature.confirmApply(cancelledApply) }
        check(fm.fileExists(atPath: cancelledReviewTarget.configRoot.path) && cancelledReviewTarget.defaults.persistentDomain(forName: cancelledReviewTarget.suiteName) == nil, "cancel apply review retains restored roots without preferences")

        let badRestore = try fixture()
        let badRestoreFeature = badRestore.controller(executor: { command in
            let raw = try await execute(command)
            guard command.operation == .restore else { return raw }
            var body = try JSONSerialization.jsonObject(with: raw) as! [String: Any]
            body["runtime_started"] = true
            return try JSONSerialization.data(withJSONObject: body)
        })
        let badRestoreArchive = try await backup(badRestore, badRestoreFeature); badRestore.restart()
        let badRestoreTarget = badRestore.target()
        let badRestoreReview = try await badRestoreFeature.prepareRestore(scope: badRestore.scope, archiveURL: badRestoreArchive, target: badRestoreTarget, otherWritersStopped: true)
        await refused(.unconfirmed) { _ = try await badRestoreFeature.confirmRestore(badRestoreReview) }
        check(fm.fileExists(atPath: badRestoreTarget.configRoot.path) && badRestoreTarget.defaults.persistentDomain(forName: badRestoreTarget.suiteName) == nil, "bad restore receipt retains roots without falsely claiming application")

        let cancelledDispatch = try fixture(); var resumeDispatch: CheckedContinuation<Void, Never>?
        let cancelledDispatchFeature = cancelledDispatch.controller(executor: { command in
            let raw = try await execute(command)
            await withCheckedContinuation { resumeDispatch = $0 }
            return raw
        })
        let cancelledDispatchArchive = cancelledDispatch.base.appendingPathComponent("cancelled-dispatch.zip")
        let cancelledDispatchReview = try cancelledDispatchFeature.prepareBackup(scope: cancelledDispatch.scope, archiveURL: cancelledDispatchArchive, otherWritersStopped: true)
        let cancelledDispatchTask = Task { try await cancelledDispatchFeature.confirmBackup(cancelledDispatchReview) }
        while resumeDispatch == nil { await Task.yield() }
        cancelledDispatchTask.cancel(); resumeDispatch?.resume()
        await refused(.unconfirmed) { _ = try await cancelledDispatchTask.value }
        check(fm.fileExists(atPath: cancelledDispatchArchive.path) && cancelledDispatch.calls == 1, "cancel after genuine publication retains archive and never retries")

        let interrupted = try fixture()
        let preferences = NativePreferenceArchiveFeature(afterPublication: { throw NativeProfileArchiveFailure.unavailable })
        let interruptedFeature = interrupted.controller(executor: execute, preferences: preferences)
        let interruptedArchive = try await backup(interrupted, interruptedFeature); interrupted.restart()
        let interruptedTarget = interrupted.target()
        let interruptedReview = try await interruptedFeature.prepareRestore(scope: interrupted.scope, archiveURL: interruptedArchive, target: interruptedTarget, otherWritersStopped: true)
        let interruptedApply = try await interruptedFeature.confirmRestore(interruptedReview)
        await refused(.unconfirmed) { _ = try await interruptedFeature.confirmApply(interruptedApply) }
        check(interruptedTarget.defaults.string(forKey: "displayName") == "Féral 👓", "post-publication interruption preserves inspectable preferences")

        let staleApply = try fixture()
        let stalePreferences = NativePreferenceArchiveFeature(afterPublication: { staleApply.restart() })
        let staleFeature = staleApply.controller(executor: execute, preferences: stalePreferences)
        let staleArchive = try await backup(staleApply, staleFeature); staleApply.restart()
        let staleTarget = staleApply.target()
        let staleReview = try await staleFeature.prepareRestore(scope: staleApply.scope, archiveURL: staleArchive, target: staleTarget, otherWritersStopped: true)
        let staleApplyReview = try await staleFeature.confirmRestore(staleReview)
        await refused(.unconfirmed) { _ = try await staleFeature.confirmApply(staleApplyReview) }
        check(staleTarget.defaults.string(forKey: "displayName") == "Féral 👓", "owner supersession after publication never claims current success or resets destination")

        // The real bounded Process executor: constructor refusal, output cap,
        // deadline, and cancellation terminate only its own synthetic CLI child.
        let fake = root.appendingPathComponent("synthetic-core", isDirectory: true)
        try fm.createDirectory(at: fake.appendingPathComponent("config"), withIntermediateDirectories: true)
        try Data().write(to: fake.appendingPathComponent("config/__init__.py"))
        let module = fake.appendingPathComponent("config/profile_archive.py")
        let childTarget = root.appendingPathComponent("child-effect")
        let command = NativeProfileArchiveCommand(operation: .restore, archiveURL: childTarget,
            configRoot: root, dataRoot: root, primarySessionID: "fixture")
        await refused(.unavailable) { _ = try await NativeProfileArchiveProcessExecutor(pythonURL: root.appendingPathComponent("missing-python"), coreURL: fake).execute(command) }
        try Data("print('x' * 100000)\n".utf8).write(to: module)
        await refused(.unconfirmed) { _ = try await NativeProfileArchiveProcessExecutor(pythonURL: python, coreURL: fake, maximumOutputBytes: 1024).execute(command) }
        try Data("import pathlib,sys,time\npathlib.Path(sys.argv[2]).write_text('started')\ntime.sleep(3)\npathlib.Path(sys.argv[2]).write_text('finished')\n".utf8).write(to: module)
        await refused(.unconfirmed) { _ = try await NativeProfileArchiveProcessExecutor(pythonURL: python, coreURL: fake, timeout: 0.2).execute(command) }
        check(try String(contentsOf: childTarget) == "started", "deadline retains actual early effect without claiming completion")
        try fm.removeItem(at: childTarget)
        let cancelled = Task { try await NativeProfileArchiveProcessExecutor(pythonURL: python, coreURL: fake).execute(command) }
        for _ in 0..<200 { if fm.fileExists(atPath: childTarget.path) { break }; try await Task.sleep(nanoseconds: 10_000_000) }
        check(fm.fileExists(atPath: childTarget.path), "actual synthetic child began before cancellation")
        cancelled.cancel()
        await refused(.unconfirmed) { _ = try await cancelled.value }
        check(try String(contentsOf: childTarget) == "started", "cancellation does not invent rollback or completion")
        print("Native profile archive: \(assertions) assertions passed")
    }
}
