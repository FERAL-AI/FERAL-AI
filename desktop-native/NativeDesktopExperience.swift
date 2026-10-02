import Foundation
import SwiftUI
import AppKit
import ServiceManagement
import CoreFoundation

private func desktopSavedBool(_ value: Any?) -> Bool? { guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }; return number.boolValue }

enum NativeDesktopLoginStatus: String { case notRegistered, enabled, requiresApproval, notFound, unknown
    var label: String { switch self { case .notRegistered: return "Not registered"; case .enabled: return "macOS reports enabled"; case .requiresApproval: return "Requires approval in System Settings"; case .notFound: return "Service not found"; case .unknown: return "Status unavailable" } }
}
@MainActor protocol NativeDesktopLoginService {
    var status: NativeDesktopLoginStatus { get }
    func register() throws
    func unregister() throws
    func openSettings()
}
@MainActor private final class NativeSystemLoginService: NativeDesktopLoginService {
    var status: NativeDesktopLoginStatus { switch SMAppService.mainApp.status { case .notRegistered: return .notRegistered; case .enabled: return .enabled; case .requiresApproval: return .requiresApproval; case .notFound: return .notFound; @unknown default: return .unknown } }
    func register() throws { try SMAppService.mainApp.register() }
    func unregister() throws { try SMAppService.mainApp.unregister() }
    func openSettings() { SMAppService.openSystemSettingsLoginItems() }
}
enum NativeDesktopAgentState: String { case starting = "Starting", ready = "Ready", unavailable = "Unavailable", stopped = "Stopped" }
enum NativeDesktopMenuAction { case show, hide, openChat, navigate, quit }
struct NativeDesktopActions {
    var isWindowVisible: () -> Bool
    var showWindow: () -> Void
    var hideWindow: () -> Void
    var openChat: () -> Void
    var showNavigation: () -> Void
    var quit: () -> Void
}
enum NativeDesktopPreferenceAction { case keepRunning(Bool), menuBar(Bool), rememberDestination(Bool), login(Bool) }
struct NativeDesktopPreferenceReview: Identifiable {
    let id = UUID(), createdUptime = ProcessInfo.processInfo.systemUptime
    let action: NativeDesktopPreferenceAction
    let oldKeep: Bool, oldMenu: Bool, oldRemember: Bool
    let oldLogin: NativeDesktopLoginStatus
    let title: String, scope: String, previous: String, proposed: String
}
@MainActor final class NativeDesktopExperience: NSObject, ObservableObject, NSMenuDelegate {
    @Published private(set) var keepRunningWhenClosed: Bool
    @Published private(set) var menuBarEnabled: Bool
    @Published private(set) var restoreLastDestination: Bool
    @Published private(set) var lastDestination: String
    @Published private(set) var loginStatus: NativeDesktopLoginStatus
    @Published private(set) var error: String?
    @Published private(set) var notice: String?
    @Published private(set) var agentState: NativeDesktopAgentState = .stopped
    let registrationEligible: Bool
    let registrationExplanation: String
    private let preferences: UserDefaults?
    var preferencesAvailable: Bool { preferences != nil }
    private static let preferencesUnavailable = "Local desktop preferences are unavailable. Desktop changes and Login Items controls are paused. Your saved settings have not been reset or changed; quit and reopen FERAL."
    private let destinations: Set<String>
    private let fallback: String
    private let login: NativeDesktopLoginService
    private var reviews: [UUID: NativeDesktopPreferenceReview] = [:]
    private var actions: NativeDesktopActions?
    private var statusItem: NSStatusItem?
    private var installed = false
    private var navigationAvailable = false
    private var menuImage: NSImage?
    private var toggleItem: NSMenuItem?
    private var stateItem: NSMenuItem?
    private var chatItem: NSMenuItem?
    private var navigationItem: NSMenuItem?
    static func resolvePreferences(suiteName: String, bundleIdentifier: String?, standard: UserDefaults, factory: (String) -> UserDefaults? = { UserDefaults(suiteName: $0) }) -> UserDefaults? {
        if suiteName == bundleIdentifier { return standard }
        guard !suiteName.isEmpty else { return nil }
        return factory(suiteName)
    }
    init(allowedDestinations: [String], fallback: String = "Chat", preferences: UserDefaults? = nil, loginService: NativeDesktopLoginService? = nil, bundleURL: URL = Bundle.main.bundleURL, homeURL: URL = FileManager.default.homeDirectoryForCurrentUser, preferenceSuite: String? = nil, bundleIdentifier: String? = Bundle.main.bundleIdentifier, standardPreferences: UserDefaults = .standard, preferenceFactory: (String) -> UserDefaults? = { UserDefaults(suiteName: $0) }) {
        let allowed = Set(allowedDestinations.filter { !$0.isEmpty && $0.utf8.count <= 128 && $0.utf8.allSatisfy { $0 >= 32 && $0 != 127 } })
        precondition(allowed.contains(fallback), "Desktop destination fallback must belong to the navigation whitelist")
        let prefs = preferences ?? Self.resolvePreferences(suiteName: preferenceSuite ?? ProcessInfo.processInfo.environment["FERAL_NATIVE_PREFS_SUITE"] ?? "ai.feral.native.preview", bundleIdentifier: bundleIdentifier, standard: standardPreferences, factory: preferenceFactory)
        self.preferences = prefs; self.destinations = allowed; self.fallback = fallback
        let service = loginService ?? NativeSystemLoginService()
        login = service; loginStatus = prefs == nil ? .unknown : service.status
        let initialMenu = prefs == nil ? false : (desktopSavedBool(prefs?.object(forKey: "desktop.menuBarEnabled")) ?? true)
        menuBarEnabled = initialMenu
        keepRunningWhenClosed = (desktopSavedBool(prefs?.object(forKey: "desktop.keepRunningWhenClosed")) ?? false) && initialMenu
        restoreLastDestination = prefs == nil ? false : (desktopSavedBool(prefs?.object(forKey: "desktop.restoreLastDestination")) ?? true)
        let saved = prefs?.string(forKey: "desktop.lastDestination") ?? fallback
        lastDestination = allowed.contains(saved) ? saved : fallback
        // Do not register a temporary build/copy as a login item. Signing and
        // actual launch behavior remain macOS-verified, not inferred from path.
        let parent = bundleURL.deletingLastPathComponent().path
        let eligible = bundleURL.isFileURL && bundleURL.pathExtension.lowercased() == "app" && (parent == "/Applications" || parent == homeURL.appendingPathComponent("Applications").path)
        registrationEligible = eligible && prefs != nil
        registrationExplanation = prefs == nil ? Self.preferencesUnavailable : (eligible ? "Installed bundle location recognized. macOS still verifies code signing and consent. This preview’s next-login launch has not been release validated." : "Launch at login is disabled for temporary/build bundles. Install the signed application directly in /Applications or ~/Applications first; release signing and a real login test remain required.")
        super.init()
        if prefs == nil { error = Self.preferencesUnavailable }
        if saved != lastDestination { prefs?.removeObject(forKey: "desktop.lastDestination") }
    }
    var initialDestination: String { restoreLastDestination ? lastDestination : fallback }
    private func reloadPreferences() {
        guard let preferences else { error = Self.preferencesUnavailable; return }
        menuBarEnabled = desktopSavedBool(preferences.object(forKey: "desktop.menuBarEnabled")) ?? true
        keepRunningWhenClosed = (desktopSavedBool(preferences.object(forKey: "desktop.keepRunningWhenClosed")) ?? false) && menuBarEnabled
        restoreLastDestination = desktopSavedBool(preferences.object(forKey: "desktop.restoreLastDestination")) ?? true
        let saved = preferences.string(forKey: "desktop.lastDestination") ?? fallback
        lastDestination = destinations.contains(saved) ? saved : fallback
        updateMenuInstallation()
    }
    func rememberDestination(_ value: String) { guard let preferences else { error = Self.preferencesUnavailable; return }; guard restoreLastDestination, destinations.contains(value) else { return }; lastDestination = value; preferences.set(value, forKey: "desktop.lastDestination") }
    func refreshLoginStatus() { guard preferencesAvailable else { error = Self.preferencesUnavailable; return }; loginStatus = login.status }
    func openLoginSettings() { guard preferencesAvailable else { error = Self.preferencesUnavailable; return }; login.openSettings(); refreshLoginStatus() }
    func review(_ action: NativeDesktopPreferenceAction) -> NativeDesktopPreferenceReview? {
        guard preferencesAvailable else { error = Self.preferencesUnavailable; return nil }
        reloadPreferences(); refreshLoginStatus(); error = nil
        let title: String, scope: String, previous: String, proposed: String
        switch action {
        case .keepRunning(let enabled):
            title = "Change background window behavior"
            scope = enabled ? "Closing the last window will keep this app and its owned brain running. Existing scheduled jobs, integrations and ongoing work may continue. The menu-bar control will also be enabled so you can reopen or quit the app. Quit still shuts down the owned brain. This does not prevent macOS sleep or keep running after logout." : "Closing the last window will quit the app through its normal owned-brain shutdown. This does not disable launch at login."
            previous = keepRunningWhenClosed ? "Keep running" : "Quit after last window closes"; proposed = enabled ? "Keep running; menu bar enabled" : "Quit after last window closes"
        case .menuBar(let enabled):
            title = "Change menu-bar access"; scope = enabled ? "Show a FERAL status menu with window, Chat, navigation and Quit actions. It does not start microphone capture or register a global shortcut." : "Remove the menu-bar control and turn off keep-running-after-close to retain a visible route to quitting the app. Launch at login is unchanged."
            previous = "Menu bar \(menuBarEnabled ? "enabled" : "disabled"); keep running \(keepRunningWhenClosed)"; proposed = enabled ? "Menu bar enabled" : "Menu bar disabled; quit after last window closes"
        case .rememberDestination(let enabled):
            title = "Remember navigation destination"; scope = "Store only a validated destination name in this preview’s preference suite. No conversation text or credentials are stored by this setting. Disabling clears the stored destination and returns subsequent launches to \(fallback)."
            previous = restoreLastDestination ? "Remember \(lastDestination)" : "Open \(fallback)"; proposed = enabled ? "Remember subsequent selections" : "Clear saved selection; open \(fallback)"
        case .login(let enabled):
            if enabled && !registrationEligible { error = registrationExplanation; return nil }
            title = enabled ? "Register launch at login" : "Unregister launch at login"
            scope = "Change this application’s macOS main-app login registration. macOS may require consent in Login Items; registration is not proof of a successful future launch. No System Settings window is opened automatically. Unregistering leaves the current app running. \(registrationExplanation)"
            previous = loginStatus.label; proposed = enabled ? "Request registration for subsequent logins" : "Remove subsequent-login registration"
        }
        let review = NativeDesktopPreferenceReview(action: action, oldKeep: keepRunningWhenClosed, oldMenu: menuBarEnabled, oldRemember: restoreLastDestination, oldLogin: loginStatus, title: title, scope: scope, previous: previous, proposed: proposed)
        reviews[review.id] = review; return review
    }
    func cancel(_ review: NativeDesktopPreferenceReview) { reviews[review.id] = nil }
    func discardReviews() { reviews = [:] }
    func perform(_ supplied: NativeDesktopPreferenceReview) -> Bool {
        guard let preferences else { reviews[supplied.id] = nil; error = Self.preferencesUnavailable; return false }
        reloadPreferences(); refreshLoginStatus(); error = nil; notice = nil
        guard let review = reviews.removeValue(forKey: supplied.id), ProcessInfo.processInfo.systemUptime - review.createdUptime <= 300, review.oldKeep == keepRunningWhenClosed, review.oldMenu == menuBarEnabled, review.oldRemember == restoreLastDestination, review.oldLogin == loginStatus else { error = "Desktop state changed or the review expired. Review again."; return false }
        do {
            switch review.action {
            case .keepRunning(let enabled):
                keepRunningWhenClosed = enabled; preferences.set(enabled, forKey: "desktop.keepRunningWhenClosed")
                if enabled { menuBarEnabled = true; preferences.set(true, forKey: "desktop.menuBarEnabled") }; updateMenuInstallation()
                notice = enabled ? "Keep-running preference saved. Quit still shuts down the owned brain." : "Close-to-quit preference saved."
            case .menuBar(let enabled):
                menuBarEnabled = enabled; preferences.set(enabled, forKey: "desktop.menuBarEnabled")
                if !enabled { keepRunningWhenClosed = false }
                preferences.set(keepRunningWhenClosed, forKey: "desktop.keepRunningWhenClosed"); updateMenuInstallation()
                notice = "Menu-bar preference saved."
            case .rememberDestination(let enabled):
                restoreLastDestination = enabled; preferences.set(enabled, forKey: "desktop.restoreLastDestination")
                if !enabled { lastDestination = fallback; preferences.removeObject(forKey: "desktop.lastDestination") }
                notice = "Destination preference saved."
            case .login(let enabled):
                guard !enabled || registrationEligible else { error = registrationExplanation; return false }
                if enabled { if loginStatus != .enabled { try login.register() } }
                else if loginStatus != .notRegistered { try login.unregister() }
                refreshLoginStatus()
                if enabled {
                    guard loginStatus == .enabled || loginStatus == .requiresApproval else { error = "Registration was requested, but macOS did not confirm enabled or pending approval. Refresh before retrying."; return false }
                    notice = loginStatus == .enabled ? "macOS reports launch at login enabled. Actual next-login launch remains untested." : "Registered but requires approval in System Settings. It is not currently eligible to launch."
                } else { guard loginStatus == .notRegistered else { error = "Unregistration was requested, but macOS did not confirm removal. Refresh before retrying."; return false }; notice = "macOS reports login registration removed. This app remains running." }
            }
            reviews = [:]; return true
        } catch { refreshLoginStatus(); self.error = "macOS did not confirm the login-item change. Code signing, installation or consent may prevent it. Private system details are withheld; refresh actual status before retrying."; return false }
    }
    func configureActions(_ actions: NativeDesktopActions) { self.actions = actions }
    func installMenuBar(image: NSImage? = nil) { installed = true; menuImage = image; updateMenuInstallation() }
    func removeMenuBar() { installed = false; if let item = statusItem { NSStatusBar.system.removeStatusItem(item) }; statusItem = nil }
    func updateAgentState(_ state: NativeDesktopAgentState, navigationAvailable: Bool) { agentState = state; self.navigationAvailable = navigationAvailable; refreshMenuLabels() }
    func performMenuAction(_ action: NativeDesktopMenuAction) {
        guard let actions else { return }
        switch action { case .show: actions.showWindow(); case .hide: actions.hideWindow(); case .openChat: guard navigationAvailable else { return }; actions.showWindow(); actions.openChat(); case .navigate: guard navigationAvailable else { return }; actions.showWindow(); actions.showNavigation(); case .quit: actions.quit() }
    }
    private func updateMenuInstallation() {
        guard installed else { return }
        guard menuBarEnabled else { if let item = statusItem { NSStatusBar.system.removeStatusItem(item) }; statusItem = nil; return }
        guard statusItem == nil else { refreshMenuLabels(); return }
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let image = menuImage { item.button?.image = image; item.button?.imagePosition = .imageLeft }
        item.button?.title = "FERAL"; item.button?.setAccessibilityLabel("FERAL desktop controls")
        let menu = NSMenu(); menu.delegate = self; menu.autoenablesItems = false
        let state = NSMenuItem(title: "", action: nil, keyEquivalent: ""); state.isEnabled = false; menu.addItem(state); stateItem = state
        menu.addItem(.separator())
        toggleItem = itemFor(menu, title: "Show FERAL", selector: #selector(toggleWindow))
        chatItem = itemFor(menu, title: "Open Chat", selector: #selector(openChat))
        navigationItem = itemFor(menu, title: "Go to…", selector: #selector(navigate))
        menu.addItem(.separator()); _ = itemFor(menu, title: "Quit FERAL", selector: #selector(quit))
        item.menu = menu; statusItem = item; refreshMenuLabels()
    }
    private func itemFor(_ menu: NSMenu, title: String, selector: Selector) -> NSMenuItem { let item = NSMenuItem(title: title, action: selector, keyEquivalent: ""); item.target = self; menu.addItem(item); return item }
    private func refreshMenuLabels() { stateItem?.title = "FERAL — \(agentState.rawValue)"; toggleItem?.title = actions?.isWindowVisible() == true ? "Hide FERAL" : "Show FERAL"; chatItem?.isEnabled = navigationAvailable; navigationItem?.isEnabled = navigationAvailable; statusItem?.button?.toolTip = "FERAL — \(agentState.rawValue)" }
    func menuWillOpen(_ menu: NSMenu) { refreshMenuLabels() }
    @objc private func toggleWindow() { performMenuAction(actions?.isWindowVisible() == true ? .hide : .show) }
    @objc private func openChat() { performMenuAction(.openChat) }
    @objc private func navigate() { performMenuAction(.navigate) }
    @objc private func quit() { performMenuAction(.quit) }
}

struct NativeDesktopExperienceView: View {
    @ObservedObject var experience: NativeDesktopExperience
    @State private var review: NativeDesktopPreferenceReview?
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack { Text("Desktop behavior").font(.title2.bold()); Spacer(); Button("Refresh macOS status") { experience.refreshLoginStatus() }.disabled(!experience.preferencesAvailable) }
            if let message = experience.error { Text(message).foregroundStyle(.red).textSelection(.enabled) }
            if let message = experience.notice { Text(message).foregroundStyle(.secondary).textSelection(.enabled) }
            preferenceRow("After the last window closes", value: experience.keepRunningWhenClosed ? "Keep app and brain running" : "Quit app and owned brain", action: .keepRunning(!experience.keepRunningWhenClosed))
            preferenceRow("Menu-bar access", value: experience.menuBarEnabled ? "Enabled" : "Disabled", action: .menuBar(!experience.menuBarEnabled))
            preferenceRow("Restore navigation", value: experience.restoreLastDestination ? "Remember validated destination: \(experience.lastDestination)" : "Open \(experience.initialDestination)", action: .rememberDestination(!experience.restoreLastDestination))
            Divider()
            Text("Launch at login: \(experience.loginStatus.label)").font(.headline)
            Text(experience.registrationExplanation).foregroundStyle(.secondary)
            HStack { Button("Review enable at login…") { review = experience.review(.login(true)) }.disabled(!experience.registrationEligible || experience.loginStatus == .enabled); Button("Review unregister…") { review = experience.review(.login(false)) }.disabled(experience.loginStatus == .notRegistered); Button("Open Login Items settings") { experience.openLoginSettings() } }.disabled(!experience.preferencesAvailable)
            Text("No global shortcut is registered. Closing a window does not undo completed agent actions. Background operation and login registration are separate preferences.").font(.caption).foregroundStyle(.secondary)
        }.padding()
        .sheet(item: $review, onDismiss: { experience.discardReviews() }) { item in VStack(alignment: .leading, spacing: 12) { Text(item.title).font(.title2.bold()); Text(item.scope).textSelection(.enabled); Text("Previous: \(item.previous)\nProposed: \(item.proposed)").textSelection(.enabled); HStack { Button("Cancel") { experience.cancel(item); review = nil }; Spacer(); Button("Confirm") { _ = experience.perform(item); review = nil } } }.padding(24).frame(width: 620) }
    }
    private func preferenceRow(_ title: String, value: String, action: NativeDesktopPreferenceAction) -> some View { HStack { VStack(alignment: .leading) { Text(title).font(.headline); Text(value).font(.caption).foregroundStyle(.secondary) }; Spacer(); Button("Review change…") { review = experience.review(action) }.accessibilityLabel("Review change: " + title).disabled(!experience.preferencesAvailable) } }
}
