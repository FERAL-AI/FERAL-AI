import SwiftUI

/// FERAL Node iOS app settings.
///
/// The About row reads ``CFBundleShortVersionString`` /
/// ``CFBundleVersion`` directly from the bundle so a release build
/// shows the exact value ``scripts/sync_versions.py`` writes into
/// ``Info.plist`` — keeping the iOS app version honest against the
/// brain's ``feral-core/pyproject.toml`` source of truth.
///
/// Intentionally no in-app debug log viewer or developer-only rows:
/// this view is rendered identically in Debug and Release. Debug
/// instrumentation lives in the host Xcode scheme's environment
/// variables (``FERAL_BRAIN_CERT_HASH``, ``OS_ACTIVITY_MODE``) and
/// the Console.app logs from ``print`` / ``NSLog`` — none of which
/// the App Store reviewer will see in a Release archive.
struct SettingsView: View {
    @EnvironmentObject var connection: ConnectionManager
    @Environment(\.dismiss) var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section("Brain Connection") {
                    TextField("Host", text: $connection.brainHost)
                        .textContentType(.URL)
                        .autocapitalization(.none)
                    TextField("Port", value: $connection.brainPort, format: .number)
                    SecureField("API Key", text: $connection.apiKey)
                    TextField("Node Name", text: $connection.nodeName)
                }

                Section {
                    Button("Connect") {
                        connection.connect()
                        dismiss()
                    }
                    .disabled(connection.brainHost.isEmpty)
                }

                Section("About") {
                    HStack {
                        Text("Version")
                        Spacer()
                        Text(Self.appVersionString)
                            .foregroundStyle(.secondary)
                            .accessibilityIdentifier("settings.about.version")
                    }
                    HStack {
                        Text("Build")
                        Spacer()
                        Text(Self.appBuildString)
                            .foregroundStyle(.secondary)
                            .accessibilityIdentifier("settings.about.build")
                    }
                }
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }

    /// Marketing version (``CFBundleShortVersionString``). Kept in
    /// sync with ``feral-core/pyproject.toml`` by
    /// ``scripts/sync_versions.py`` on every release bump.
    static var appVersionString: String {
        (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "unknown"
    }

    /// Build number (``CFBundleVersion``). Increments per archive
    /// independent of the marketing version.
    static var appBuildString: String {
        (Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String) ?? "unknown"
    }
}
