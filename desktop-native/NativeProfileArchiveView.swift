import SwiftUI
import AppKit
import UniformTypeIdentifiers

/// Offline archive controls for the currently verified installation. Restored
/// roots and their fresh preference suite are never selected as the active app.
struct NativeProfileArchiveView: View {
    @ObservedObject var model: NativeModel
    @ObservedObject var archive: NativeProfileArchiveFeature
    @State private var otherWritersStopped = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Backup & restore").font(.headline)
            Text("Keep a backup of your conversations, memory and profile. Restore a backup of this installation into a new folder without replacing your current profile.")
                .font(.callout).foregroundStyle(.secondary)
            if let review = archive.review {
                reviewPanel(review)
            } else if let review = archive.applyReview {
                applyPanel(review)
            } else {
                Toggle("All other apps and commands using this profile are stopped", isOn: $otherWritersStopped)
                    .disabled(archive.busy || model.profileArchivePaused)
                HStack {
                    Button("Back up…", action: chooseBackup)
                    Button("Restore to a new folder…", action: chooseRestore)
                }.disabled(!model.canPrepareProfileArchive || !otherWritersStopped || archive.busy)
            }
            if archive.busy {
                HStack { ProgressView().controlSize(.small); Text("Verifying the selected profile and archive…") }
            }
            if let receipt = archive.receipt {
                VStack(alignment: .leading, spacing: 6) {
                    Label(receipt.operation == .backup ? "Backup verified" : (receipt.preferencesApplied ? "Restore and preferences verified" : "Profile files restored"), systemImage: "checkmark.circle")
                        .foregroundStyle(.green)
                    NativeSelectableText(receipt.archiveURL.path).font(.caption)
                    if receipt.operation != .backup {
                        NativeSelectableText(receipt.configRoot.path).font(.caption)
                        Text("The restored profile is not running. Your original profile remains selected.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    DisclosureGroup("Verification details") {
                        NativeSelectableText("Installation: " + receipt.primarySessionID + "\nArchive SHA-256: " + receipt.archiveSHA256 + "\nPreferences: " + receipt.suiteName)
                            .font(.system(.caption, design: .monospaced))
                    }
                }
            }
            if let error = model.profileArchiveError ?? archive.failure?.localizedDescription {
                NativeSelectableText(error).font(.callout).foregroundStyle(.red)
            }
            if model.profileArchivePaused {
                Text("This app's local agent is stopped. Restarting the original profile does not activate the restored copy or repeat earlier tasks.")
                    .font(.caption).foregroundStyle(.secondary)
                Button("Restart original profile") { Task { await model.resumeOriginalProfile() } }
                    .disabled(!model.canResumeOriginalProfile)
            }
            Text("Backups can contain private information and encrypted credentials. Keep them somewhere you trust. Device permissions and OS keys stay on this Mac.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private func reviewPanel(_ review: NativeProfileArchiveReview) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(review.operation == .backup ? "Review backup" : "Review restore").font(.headline)
            NativeSelectableText(review.archiveURL.path).font(.caption)
            if review.operation == .restore {
                Text("New profile folder:").font(.caption)
                NativeSelectableText(review.configRoot.path).font(.caption)
            }
            Text("FERAL will stop its local agent while it works. All other writers must already be stopped. Preferences need a separate confirmation after restore.")
                .font(.callout)
            HStack {
                Button(review.operation == .backup ? "Stop agent & back up" : "Stop agent & restore") {
                    model.performProfileArchive {
                        if review.operation == .backup { _ = try await archive.confirmBackup(review) }
                        else { _ = try await archive.confirmRestore(review) }
                    }
                }.buttonStyle(.borderedProminent)
                Button("Cancel") { model.performProfileArchive { try archive.cancelReview() } }
            }.disabled(archive.busy)
        }.padding(14).background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 10))
    }

    private func applyPanel(_ review: NativeProfileArchiveApplyReview) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Restore preferences?").font(.headline)
            Text("Apply the archived companion, appearance and conversation preferences to the restored copy. Your current app preferences are unchanged.")
                .font(.callout)
            NativeSelectableText(review.configRoot.path).font(.caption)
            Text("\(review.preferenceKeys.count) saved preference keys" + (review.includesAvatar ? " and your imported avatar" : ""))
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Button("Apply to restored copy") {
                    model.performProfileArchive { _ = try await archive.confirmApply(review) }
                }.buttonStyle(.borderedProminent)
                Button("Keep restored files only") { model.performProfileArchive { try archive.cancelReview() } }
            }.disabled(archive.busy)
        }.padding(14).background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 10))
    }

    private func chooseBackup() {
        let panel = NSSavePanel()
        panel.title = "Choose where to save your FERAL backup"
        panel.nameFieldStringValue = "FERAL Profile.zip"
        panel.allowedContentTypes = [.zip]
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let url = panel.url else { return }
        model.performProfileArchive {
            let scope = try model.profileArchiveScope()
            _ = try archive.prepareBackup(scope: scope, archiveURL: url, otherWritersStopped: otherWritersStopped)
        }
    }

    private func chooseRestore() {
        let source = NSOpenPanel()
        source.title = "Choose a backup of this FERAL installation"
        source.allowedContentTypes = [.zip]
        source.allowsMultipleSelection = false
        source.canChooseDirectories = false
        guard source.runModal() == .OK, let archiveURL = source.url else { return }
        let destination = NSSavePanel()
        destination.title = "Choose a new folder for the restored profile"
        destination.nameFieldStringValue = "FERAL Restored Profile"
        destination.canCreateDirectories = true
        guard destination.runModal() == .OK, let root = destination.url else { return }
        model.performProfileArchive {
            let scope = try model.profileArchiveScope()
            let suite = "ai.feral.restored." + UUID().uuidString.lowercased()
            guard let defaults = UserDefaults(suiteName: suite) else { throw NativeProfileArchiveFailure.unavailable }
            let target = NativeProfileArchiveTarget(configRoot: root, dataRoot: root, defaults: defaults,
                suiteName: suite, primarySessionID: scope.primarySessionID)
            _ = try await archive.prepareRestore(scope: scope, archiveURL: archiveURL, target: target,
                otherWritersStopped: otherWritersStopped)
        }
    }
}
