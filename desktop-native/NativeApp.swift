import SwiftUI
import AppKit

@main struct FeralNativeApp: App {
    @NSApplicationDelegateAdaptor(FeralAppDelegate.self) private var delegate
    @StateObject private var model = NativeModel()
    @StateObject private var desktop = NativeDesktopExperience(allowedDestinations: NativeDestination.allCases.map(\.rawValue))
    var body: some Scene {
        WindowGroup("FERAL Native Preview", id: "feral-main") {
            NativeRootView(model: model, desktop: desktop, host: delegate)
                .frame(minWidth: 820, minHeight: 620)
                .task { if model.onboarded { await model.start() } }
        }
        .defaultSize(width: 1180, height: 800)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New Conversation") { model.newConversation() }.keyboardShortcut("n")
            }
            CommandMenu("Navigate") {
                Button("Go to…") { delegate.navigate(.palette) }
                    .keyboardShortcut("k").disabled(!model.onboarded)
                Divider()
                ForEach(NativeDestination.allCases) { destination in
                    Button(destination.rawValue) { delegate.navigate(.destination(destination)) }
                        .disabled(!model.onboarded)
                }
            }
        }
    }
}
