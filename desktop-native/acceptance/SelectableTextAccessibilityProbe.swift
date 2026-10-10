// Isolate native accessibility behavior without a backend or personal profile.
import SwiftUI
import AppKit

@main struct SelectableTextAccessibilityProbe: App {
    var body: some Scene {
        WindowGroup("FERAL Accessibility Probe") { ProbeContent() }
    }
}

private struct ProbeContent: View {
    @State private var expanded = false
    @State private var rich = true
    @State private var draft = ""
    private let mode = ProcessInfo.processInfo.environment["FERAL_AX_PROBE_MODE"] ?? "selectable"
    private let status = "Separate conversation connected. Available missed messages recovered for this thread. Saved history was not injected into model context; shared conversation context is not inherited automatically."
    var body: some View {
        NavigationSplitView {
            List {
                Button("Rich conversation") { rich = true }
                Button("Short conversation") { rich = false }
            }.navigationTitle("FERAL")
        } detail: {
        VStack(alignment: .leading, spacing: 16) {
            Text("FERAL Accessibility Probe").font(.title)
            Text("Synthetic text only. Mode: " + mode)
            DisclosureGroup("Conversation connection", isExpanded: $expanded) {
                if mode == "appkit" {
                    NativeSelectableText(text: status)
                } else if mode == "plain" {
                    Text(status).font(.caption).foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else if mode == "explicit-label" {
                    Text(status).font(.caption).foregroundStyle(.secondary)
                        .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                        .accessibilityLabel(status)
                } else {
                    Text(status).font(.caption).foregroundStyle(.secondary)
                        .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 24) {
                        if mode == "appkit" {
                            NativeRichText(text: "Formatting: **bold**, *italic*, `inline code`, [Public test link](https://example.com).\n```swift\nlet synthetic = true\n```")
                        }
                        ForEach(0..<(rich ? 20 : 2), id: \.self) { index in
                            VStack(alignment: .leading, spacing: 6) {
                                Text(index % 2 == 0 ? "You" : "FERAL").foregroundStyle(.secondary)
                                display("Synthetic conversation message \(index). " + String(repeating: "Only local test text. ", count: rich ? 15 : 1))
                                DisclosureGroup("Inspect exact metadata") {
                                    display("{\"synthetic\":true,\"index\":\(index)}")
                                }
                            }.id(index)
                        }
                    }.padding(24).frame(maxWidth: 850).frame(maxWidth: .infinity)
                }.frame(maxWidth: .infinity, maxHeight: .infinity).layoutPriority(1)
                .onChange(of: rich) { _ in proxy.scrollTo(rich ? 19 : 1, anchor: .bottom) }
            }
            TextEditor(text: $draft).frame(height: 64).accessibilityLabel("Message probe")
            Text("No tools, accounts, network or model requests.")
        }.padding(24)
        }.frame(width: 1000, height: 650)
    }

    @ViewBuilder private func display(_ text: String) -> some View {
        if mode == "appkit" { NativeSelectableText(text: text) }
        else if mode == "plain" { Text(text) }
        else if mode == "explicit-label" { Text(text).textSelection(.enabled).accessibilityLabel(text) }
        else { Text(text).textSelection(.enabled) }
    }
}
