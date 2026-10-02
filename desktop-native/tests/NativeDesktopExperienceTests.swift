import Foundation
@MainActor private final class DesktopLoginFake: NativeDesktopLoginService {
    var status: NativeDesktopLoginStatus = .notRegistered
    var registers = 0, unregisters = 0, opens = 0
    var fail = false, approval = false, missing = false
    func register() throws { registers += 1; if fail { throw NSError(domain: "fixture", code: 1, userInfo: [NSLocalizedDescriptionKey: "private-system-sentinel"]) }; status = missing ? .notFound : (approval ? .requiresApproval : .enabled) }
    func unregister() throws { unregisters += 1; if fail { throw NSError(domain: "fixture", code: 1) }; status = .notRegistered }
    func openSettings() { opens += 1 }
}
@main private struct NativeDesktopExperienceTests {
    @MainActor static func main() {
        var count = 0
        func check(_ condition: Bool, _ label: String) { precondition(condition, label); count += 1 }
        let suite = "ai.feral.native.preview.desktop-fixture-" + UUID().uuidString
        let prefs = UserDefaults(suiteName: suite)!; defer { prefs.removePersistentDomain(forName: suite) }
        let login = DesktopLoginFake()
        let allowed = ["Chat", "Memory", "Settings"]
        let controller = NativeDesktopExperience(allowedDestinations: allowed, preferences: prefs, loginService: login, bundleURL: URL(fileURLWithPath: "/private/tmp/FERAL Native Preview.app"))
        check(!controller.keepRunningWhenClosed && controller.menuBarEnabled && controller.initialDestination == "Chat", "fresh defaults preserve opt-in background semantics")
        check(login.registers == 0 && login.opens == 0 && !controller.registrationEligible, "construction cannot register/open Settings; temporary bundle gated")
        check(controller.review(.login(true)) == nil && login.registers == 0, "temporary preview cannot register as login item")
        controller.rememberDestination("Memory"); check(controller.lastDestination == "Memory" && prefs.string(forKey: "desktop.lastDestination") == "Memory", "validated destination persisted in isolated suite")
        controller.rememberDestination("private-invalid-sentinel"); check(controller.lastDestination == "Memory", "unknown destination cannot be persisted")
        let keep = controller.review(.keepRunning(true))!
        check(!controller.keepRunningWhenClosed && keep.scope.contains("scheduled jobs") && keep.proposed.contains("menu bar enabled"), "review alone cannot enable background; scope disclosed")
        check(controller.perform(keep) && controller.keepRunningWhenClosed, "explicit opt-in saves keep-running behavior")
        check(!controller.perform(keep), "preference review single-use")
        let noMenu = controller.review(.menuBar(false))!; check(controller.perform(noMenu) && !controller.menuBarEnabled && !controller.keepRunningWhenClosed, "disabling menu disables close-to-background too")
        let keepAgain = controller.review(.keepRunning(true))!; check(controller.perform(keepAgain) && controller.menuBarEnabled, "background opt-in restores menu access")
        let canceled = controller.review(.keepRunning(false))!; controller.cancel(canceled); check(!controller.perform(canceled) && controller.keepRunningWhenClosed, "canceled preference cannot execute")
        let disableRemember = controller.review(.rememberDestination(false))!; check(controller.perform(disableRemember) && controller.initialDestination == "Chat" && prefs.string(forKey: "desktop.lastDestination") == nil, "turning restoration off clears saved selection")
        controller.rememberDestination("Memory"); check(controller.lastDestination == "Chat", "disabled restoration does not retain new selections")
        let externalChange = controller.review(.keepRunning(false))!; prefs.set(false, forKey: "desktop.menuBarEnabled")
        check(!controller.perform(externalChange), "external preference changes invalidate old/new review")
        prefs.set("true", forKey: "desktop.keepRunningWhenClosed"); prefs.set(true, forKey: "desktop.menuBarEnabled"); prefs.set("invalid-destination", forKey: "desktop.lastDestination")
        let malformed = NativeDesktopExperience(allowedDestinations: allowed, preferences: prefs, loginService: login, bundleURL: URL(fileURLWithPath: "/private/tmp/FERAL.app"))
        check(!malformed.keepRunningWhenClosed && malformed.lastDestination == "Chat", "malformed boolean cannot opt in; invalid destination reset")
        let installed = NativeDesktopExperience(allowedDestinations: allowed, preferences: prefs, loginService: login, bundleURL: URL(fileURLWithPath: "/Applications/FERAL.app"))
        check(installed.registrationEligible && login.registers == 0, "installed path eligibility does not claim signing or register automatically")
        login.approval = true
        let enable = installed.review(.login(true))!; check(installed.perform(enable) && installed.loginStatus == .requiresApproval && login.opens == 0, "pending approval is not reported enabled or auto-opened")
        check(installed.notice?.contains("not currently eligible") == true, "pending approval receipt accurate")
        installed.openLoginSettings(); check(login.opens == 1, "System Settings only explicit action")
        let remove = installed.review(.login(false))!; check(installed.perform(remove) && login.unregisters == 1 && installed.loginStatus == .notRegistered, "unregister verified without quitting")
        login.approval = false; login.fail = true
        let failed = installed.review(.login(true))!; check(!installed.perform(failed) && installed.error?.contains("private-system-sentinel") == false, "system failures private and no false enablement")
        login.fail = false; login.missing = true
        let missing = installed.review(.login(true))!; check(!installed.perform(missing) && installed.loginStatus == .notFound, "missing-service registration not success")
        login.missing = false; login.status = .notRegistered
        let changedOS = installed.review(.login(true))!; login.status = .enabled; let before = login.registers; check(!installed.perform(changedOS) && login.registers == before, "fresh OS state required before mutation")
        var menuCalls: [String] = []
        installed.configureActions(NativeDesktopActions(isWindowVisible: { false }, showWindow: { menuCalls.append("show") }, hideWindow: { menuCalls.append("hide") }, openChat: { menuCalls.append("chat") }, showNavigation: { menuCalls.append("navigate") }, quit: { menuCalls.append("quit") }))
        installed.performMenuAction(.openChat); installed.performMenuAction(.navigate); check(menuCalls.isEmpty, "onboarding blocks menu navigation")
        installed.updateAgentState(.ready, navigationAvailable: true)
        installed.performMenuAction(.openChat); installed.performMenuAction(.navigate); installed.performMenuAction(.hide); installed.performMenuAction(.quit)
        check(menuCalls == ["show", "chat", "show", "navigate", "hide", "quit"], "menu actions routed to injected native owner and no voice activation")
        check(installed.agentState == .ready && login.opens == 1, "status update cannot request permissions")
        print("Native desktop experience: \(count) assertions passed")
    }
}
