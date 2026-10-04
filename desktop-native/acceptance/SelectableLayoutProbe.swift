// Synthetic selectable-text constraint reproduction, no backend or user data.
import SwiftUI
import AppKit

@main struct SelectableLayoutProbe: App {
    var body: some Scene { WindowGroup("FERAL Selectable Layout Probe") { LayoutProbeContent() } }
}

private struct LayoutProbeContent: View {
    @State private var section = "vault"
    @State private var narrow = false
    @State private var draft = ""
    private let fixed = ProcessInfo.processInfo.environment["FERAL_LAYOUT_MODE"] == "measured-copy"
    private let path = "/private/tmp/feral-native-populated-20261002/project"
    var body: some View {
        NavigationSplitView {
            List { Text("Synthetic Security and Cost") }
        } detail: {
            TabView(selection: $section) {
                Text("Synthetic vault status. No account, keychain or runtime.")
                    .tabItem { Label("Credential vault", systemImage: "lock.shield") }.tag("vault")
                Group {
                if let raw = ProcessInfo.processInfo.environment["FERAL_LAYOUT_SECURITY_URL"], let url = URL(string: raw) {
                    NativeSecurityFeatureView(baseURL: url)
                } else { VStack(alignment: .leading, spacing: 16) {
                    HStack { Text("Security and cost").font(.title2.bold()); Spacer(); Button("Toggle width") { narrow.toggle() } }
                    ScrollView {
                        VStack(alignment: .leading, spacing: 14) {
                            Text("Explicit workspace grants").font(.headline)
                            HStack {
                                VStack(alignment: .leading) {
                                    display(path)
                                    Text("readwrite").font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Button("Harmless inspect") { draft = path }
                            }
                            Divider()
                            display(String(repeating: "Unicode 👩🏽‍💻 café العربية 漢字 long wrapping. ", count: 16))
                            display("**Exact diagnostic**, no interpreted links.").font(.caption)
                            TextField("Copy verification", text: $draft)
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }
                }.padding(20).frame(maxWidth: narrow ? 300 : .infinity) }
                }
                    .tabItem { Label("Permissions and cost", systemImage: "checkmark.shield") }.tag("security")
            }
        }.frame(width: 1000, height: 650)
    }
    @ViewBuilder private func display(_ text: String) -> some View {
        if fixed { MeasuredCopyProbeText(text: text) }
        else { NativeSelectableText(text) }
    }
}

private struct MeasuredCopyProbeText: NSViewRepresentable {
    let text: String
    func makeNSView(context: Context) -> NSTextField { NativeSelectableTextField.makeField(text: text) }
    func updateNSView(_ field: NSTextField, context: Context) {
        if field.stringValue != text { field.stringValue = text }
        field.setAccessibilityLabel(text)
    }
    func sizeThatFits(_ proposal: ProposedViewSize, nsView field: NSTextField, context: Context) -> CGSize? {
        let width = max(1, proposal.width ?? 600)
        guard let cell = field.cell?.copy() as? NSTextFieldCell else { return nil }
        let size = cell.cellSize(forBounds: NSRect(x: 0, y: 0, width: width, height: .greatestFiniteMagnitude))
        return CGSize(width: width, height: max(1, ceil(size.height)))
    }
}
