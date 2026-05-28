import SwiftUI

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
            }
            .navigationTitle("Settings")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }
}
