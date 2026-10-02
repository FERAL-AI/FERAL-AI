import SwiftUI
import AppKit

struct NativeDesktopIntent: Identifiable {
    enum Action { case destination(NativeDestination), palette }
    let id = UUID()
    let action: Action
}

@MainActor final class FeralAppDelegate: NSObject, ObservableObject, NSApplicationDelegate {
    var model: NativeModel?
    var desktop: NativeDesktopExperience?
    @Published private(set) var pendingIntent: NativeDesktopIntent?
    private weak var mainWindow: NSWindow?
    private var reopen: (() -> Void)?
    private var closeObserver: NSObjectProtocol?
    private var quitting = false

    func configure(model: NativeModel, desktop: NativeDesktopExperience, reopen: @escaping () -> Void) {
        self.model = model; self.desktop = desktop; self.reopen = reopen
        desktop.configureActions(NativeDesktopActions(
            isWindowVisible: { [weak self] in self?.mainWindow?.isVisible == true },
            showWindow: { [weak self] in self?.showWindow() },
            hideWindow: { [weak self] in self?.mainWindow?.orderOut(nil) },
            openChat: { [weak self] in self?.navigate(.destination(.chat)) },
            showNavigation: { [weak self] in self?.navigate(.palette) },
            quit: { NSApplication.shared.terminate(nil) }))
        desktop.installMenuBar()
        refreshState()
    }
    func attach(_ window: NSWindow) {
        guard mainWindow !== window else { return }
        if let closeObserver { NotificationCenter.default.removeObserver(closeObserver) }
        mainWindow = window
        closeObserver = NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: window, queue: .main) { [weak self, weak window] _ in
            Task { @MainActor in if self?.mainWindow === window { self?.mainWindow = nil } }
        }
    }
    func showWindow() {
        guard !quitting else { return }
        NSApplication.shared.activate(ignoringOtherApps: true)
        if let window = mainWindow { window.makeKeyAndOrderFront(nil); window.deminiaturize(nil) }
        else { reopen?() }
    }
    func navigate(_ action: NativeDesktopIntent.Action) {
        guard model?.onboarded == true, !quitting else { return }
        pendingIntent = NativeDesktopIntent(action: action)
        showWindow()
    }
    func consumeIntent(_ id: UUID) { if pendingIntent?.id == id { pendingIntent = nil } }
    func refreshState() {
        guard let model, let desktop else { return }
        desktop.updateAgentState(model.ready ? .ready : (model.busy ? .starting : .unavailable), navigationAvailable: model.onboarded)
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if quitting { return .terminateNow }
        quitting = true; desktop?.updateAgentState(.stopped, navigationAvailable: false); desktop?.removeMenuBar()
        Task { await model?.shutdown(); sender.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { desktop?.keepRunningWhenClosed != true }
}

struct NativeDesktopWindowCapture: NSViewRepresentable {
    let attach: (NSWindow) -> Void
    final class Capture: NSView {
        var attach: ((NSWindow) -> Void)?
        override func viewDidMoveToWindow() { super.viewDidMoveToWindow(); if let window { attach?(window) } }
    }
    func makeNSView(context: Context) -> Capture { let view = Capture(); view.attach = attach; return view }
    func updateNSView(_ view: Capture, context: Context) { view.attach = attach; if let window = view.window { attach(window) } }
}
