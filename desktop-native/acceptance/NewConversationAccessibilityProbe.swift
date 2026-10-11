// Synthetic navigation/error-banner reproduction. No runtime or personal data.
import SwiftUI
import AppKit

@main struct NewConversationAccessibilityProbe: App {
    var body: some Scene {
        WindowGroup("FERAL New Conversation Probe") { NewConversationProbeContent() }
    }
}

private struct NewConversationProbeContent: View {
    @State private var conversation = false
    @State private var chatError: String? = "supervisor_paused"
    @State private var draft = ""
    private let mode = ProcessInfo.processInfo.environment["FERAL_AX_PROBE_MODE"] ?? "original"
    private let error = "supervisor_paused"

    var body: some View {
        NavigationSplitView {
            List {
                Button("Conversations") { conversation = false }
                Button("New conversation") { chatError = nil; conversation = true }
            }
        } detail: {
            VStack(alignment: .leading, spacing: 12) {
                if !conversation || chatError != error { banner }
                if conversation {
                    DisclosureGroup("Conversation connection") {
                        NativeSelectableText(text: "New isolated synthetic conversation.")
                    }
                    Spacer()
                    Text("How can I help, Acceptance Tester?").font(.title)
                    Text("Synthetic navigation only; no backend, account or network.")
                    Spacer()
                    TextEditor(text: $draft).frame(height: 64).accessibilityLabel("Message probe")
                } else {
                    Text("Conversations").font(.title)
                    Button("New conversation") { chatError = nil; conversation = true }
                    ScrollView {
                        Text("Synthetic saved conversation").font(.title2)
                        NativeSelectableText(text: "Synthetic saved refusal: supervisor_paused")
                    }
                }
            }.padding(24)
        }.frame(width: 1000, height: 650)
    }

    private var banner: some View {
        VStack(alignment: .leading, spacing: 6) {
            if mode == "appkit" {
                HStack(alignment: .top) {
                    Image(systemName: "exclamationmark.circle").accessibilityHidden(true)
                    NativeSelectableText(NativeErrorPresentation.summary(error)).font(.callout)
                        .foregroundStyle(.red).lineLimit(2)
                        .accessibilityLabel("Error: " + NativeErrorPresentation.summary(error))
                }.accessibilityElement(children: .contain)
            } else {
                Label(NativeErrorPresentation.summary(error), systemImage: "exclamationmark.circle")
                    .lineLimit(2).textSelection(.enabled)
                    .accessibilityLabel("Error: " + NativeErrorPresentation.summary(error))
            }
            DisclosureGroup("Error details") {
                ScrollView([.horizontal, .vertical]) {
                    if mode == "appkit" {
                        NativeSelectableText(verbatim: error).font(.system(.caption, design: .monospaced))
                            .foregroundStyle(.secondary).accessibilityLabel("Exact error details: " + error)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    } else {
                        Text(verbatim: error).font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .accessibilityLabel("Exact error details: " + error)
                    }
                }.frame(height: 80)
            }.font(.caption).foregroundStyle(.secondary)
        }.font(.callout).foregroundStyle(Color.red).padding(10)
            .frame(maxWidth: .infinity, alignment: .leading).background(Color.red.opacity(0.06))
            .accessibilityElement(children: .contain)
    }
}
