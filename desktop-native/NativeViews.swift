import SwiftUI
import AppKit
import UniformTypeIdentifiers

extension Notification.Name {
    static let feralShowNavigation = Notification.Name("ai.feral.native.showNavigation")
    static let feralNavigate = Notification.Name("ai.feral.native.navigate")
}

struct NativeMessage: Identifiable {
    var id: String
    var role: String
    var text: String
    var metadata: [String: Any] = [:]

    var savedRecord: [String: Any] {
        var row = metadata
        if row["id"] == nil { row["id"] = id }
        if row["role"] == nil { row["role"] = role }
        if metadata.isEmpty || row["text"] is String { row["text"] = text }
        if metadata.isEmpty || row["content"] is String { row["content"] = text }
        if row["text"] == nil && row["content"] == nil && !text.isEmpty { row["content"] = text }
        return row
    }

    static func restored(_ row: [String: Any]) -> NativeMessage {
        NativeMessage(id: row["id"] as? String ?? UUID().uuidString,
                      role: row["role"] as? String ?? "unknown",
                      text: row["content"] as? String ?? row["text"] as? String ?? "",
                      metadata: row)
    }
}

struct NativeAction: Identifiable {
    var id: String
    var title: String
    var status: String
    var detail: String
}

struct NativePermission: Identifiable {
    var id: String
    var title: String
    var before: String = ""
    var after: String = ""
    var diff: String = ""
    var reviewable = false
}

struct NativeCodingState {
    var workspace: String = ""
    var endpoint: String = ""
    var modelName: String = ""
    var prepared: Bool = false
    var engineReady: Bool = false
    var status: String = ""
    var text: String = ""
    var actions: [NativeAction] = []
    var permissions: [NativePermission] = []
}

struct NativeMemory: Identifiable {
    var id: String
    var title: String
    var detail: String
}

struct NativeDevice: Identifiable {
    var id: String
    var title: String
    var detail: String
}

enum NativeDestination: String, CaseIterable, Identifiable {
    case home = "Home", chat = "Chat", conversations = "Conversations", voice = "Voice", coding = "Coding", health = "Health", memory = "Memory", knowledge = "Knowledge", memoryContext = "Memory Context", identity = "Identity", agents = "Specialists", workflows = "Workflows", automation = "Automation", apps = "Apps", capabilities = "Skills & Store", oversight = "Oversight", operations = "Activity", devices = "Devices", providers = "AI Providers", security = "Security & Cost", settings = "Settings"
    var id: String { rawValue }
    var icon: String {
        switch self {
        case .home: return "house"
        case .chat: return "bubble.left.and.bubble.right"
        case .conversations: return "clock"
        case .providers: return "cpu"
        case .operations: return "list.bullet.rectangle"
        case .voice: return "waveform"
        case .security: return "lock.shield"
        case .coding: return "curlybraces"
        case .health: return "heart"
        case .memory: return "brain"
        case .knowledge: return "books.vertical"
        case .memoryContext: return "list.bullet.indent"
        case .agents: return "person.3"
        case .identity: return "person.crop.circle"
        case .workflows: return "arrow.triangle.branch"
        case .automation: return "bolt.badge.clock"
        case .apps: return "app.badge"
        case .capabilities: return "square.grid.2x2"
        case .oversight: return "checkmark.shield"
        case .devices: return "eyeglasses"
        case .settings: return "slider.horizontal.3"
        }
    }
}

private let nativeAccent = Color(red: 0.54, green: 0.29, blue: 0.23)

struct NativeRootView: View {
    @ObservedObject var model: NativeModel
    @ObservedObject var desktop: NativeDesktopExperience
    @ObservedObject var host: FeralAppDelegate
    @Environment(\.openWindow) private var openWindow

    init(model: NativeModel, desktop: NativeDesktopExperience, host: FeralAppDelegate) {
        self.model = model; self.desktop = desktop; self.host = host
        _selection = State(initialValue: NativeDestination(rawValue: desktop.initialDestination) ?? .chat)
    }
    @State private var selection: NativeDestination? = .chat
    @State private var navigationPresented = false
    @State private var navigationQuery = ""
    @FocusState private var navigationFocused: Bool

    private var matchingDestinations: [NativeDestination] {
        let query = navigationQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        return NativeDestination.allCases.filter { query.isEmpty || $0.rawValue.localizedCaseInsensitiveContains(query) }
    }

    var body: some View {
        Group {
            if !model.onboarded {
                NativeOnboardingView(model: model)
            } else if model.showProviderSetup {
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        NativeBrandMark(size: 32)
                        Text("Your companion is ready to configure").font(.headline)
                        Spacer()
                        Button("Set up later") { model.dismissProviderSetup() }
                    }.padding(.horizontal, 24).padding(.top, 18)
                    if !model.ready {
                        NativeStartupStatus(model: model, openSecurity: { model.dismissProviderSetup(); selection = .security }).padding(.horizontal, 24)
                    }
                    ScrollView {
                        NativeOnboardingSetupFeatureView(baseURL: model.featureBaseURL, onCompleted: { model.completeProviderSetup() })
                    }
                }
            } else {
                NavigationSplitView {
                    VStack(alignment: .leading, spacing: 20) {
                        HStack(spacing: 12) {
                            NativeBrandMark(size: 38)
                            VStack(alignment: .leading, spacing: 3) {
                                Text("FERAL").font(.headline)
                                Text("Native Preview").font(.caption2.weight(.medium)).foregroundStyle(.secondary)
                            }
                        }.padding(.horizontal, 14).padding(.top, 18)
                        HStack(spacing: 10) {
                            NativeAvatar(choice: model.avatarChoice, importedPath: model.importedAvatarPath, size: 32)
                            Text(model.displayName.isEmpty ? "Your companion" : "Here for \(model.displayName)")
                                .font(.caption).foregroundStyle(.secondary).lineLimit(2)
                        }.padding(.horizontal, 14)
                        List(selection: $selection) {
                            Section("Assistant") { navigationRows([.home, .chat, .conversations, .voice, .coding]) }
                            Section("Personal") { navigationRows([.health, .memory, .knowledge, .memoryContext, .identity, .agents]) }
                            Section("Work & tools") { navigationRows([.workflows, .automation, .apps, .capabilities, .oversight, .operations]) }
                            Section("Connections & settings") { navigationRows([.devices, .providers, .security, .settings]) }
                        }.listStyle(.sidebar)
                        HStack(spacing: 7) {
                            Circle().fill(model.ready ? Color.green : Color.orange).frame(width: 6, height: 6)
                            Text(model.ready ? "Local agent connected" : model.startupStatus)
                                .font(.caption).foregroundStyle(.secondary).lineLimit(2)
                        }.padding(.horizontal, 16).padding(.bottom, 18)
                    }.navigationSplitViewColumnWidth(min: 190, ideal: 215, max: 260)
                } detail: {
                    VStack(spacing: 0) {
                        if let warning = model.runtimeHealthWarning {
                            Label(warning, systemImage: "clock").font(.callout).foregroundStyle(.secondary)
                                .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                        }
                        if let error = model.error, !error.isEmpty,
                           (selection ?? .chat) != .chat || model.chatError != error { NativeErrorBanner(text: error) }
                        if !model.ready {
                            NativeStartupStatus(model: model, openSecurity: { selection = .security }).padding(12).frame(maxWidth: .infinity)
                        }
                        switch selection ?? .chat {
                        case .home: NativeAmbientFeatureView(baseURL: model.featureBaseURL)
                        case .chat: NativeChatView(model: model)
                        case .voice: NativeVoiceFeatureView(baseURL: model.featureBaseURL, engine: model.voice)
                        case .conversations:
                            NativeConversationFeatureView(baseURL: model.featureBaseURL, onOpen: { id in
                                Task { await model.openConversation(id); if model.activeConversationID == id && model.error == nil { selection = .chat } }
                            }, onNew: { model.newConversation(); selection = .chat }, onDeleted: { model.conversationDeleted($0) }, canDelete: { !model.switchingConversation && $0 != model.activeConversationID })
                        case .providers:
                            NativeProvidersFeatureView(baseURL: model.featureBaseURL, onConfigurationChanged: { Task { await model.refreshProviderConfiguration() } })
                        case .coding: NativeCodingView(model: model)
                        case .health:
                            TabView {
                                NativeHealthFeatureView(baseURL: model.featureBaseURL).tabItem { Label("Overview", systemImage: "heart") }
                                NativeHealthHistoryFeatureView(baseURL: model.featureBaseURL).tabItem { Label("Recorded history", systemImage: "chart.xyaxis.line") }
                            }
                        case .memory: NativeMemoryFeatureView(baseURL: model.featureBaseURL)
                        case .knowledge: NativeKnowledgeFeatureView(baseURL: model.featureBaseURL)
                        case .memoryContext: NativeMemoryContextFeatureView(baseURL: model.featureBaseURL)
                        case .agents: NativeAgentFeatureView(baseURL: model.featureBaseURL, sessionID: model.activeConversationID.isEmpty ? nil : model.activeConversationID)
                        case .identity: NativeIdentityFeatureView(baseURL: model.featureBaseURL)
                        case .workflows: NativeWorkflowFeatureView(baseURL: model.featureBaseURL, sessionID: model.activeConversationID.isEmpty ? nil : model.activeConversationID)
                        case .automation: NativeAutomationFeatureView(baseURL: model.featureBaseURL, sessionID: model.activeConversationID.isEmpty ? nil : model.activeConversationID)
                        case .apps: NativeAppSurfaceFeatureView(baseURL: model.featureBaseURL, sessionID: model.appSurfaceSessionID)
                        case .capabilities: NativeCapabilitiesFeatureView(baseURL: model.featureBaseURL)
                        case .oversight: NativeOversightFeatureView(baseURL: model.featureBaseURL)
                        case .operations:
                            NativeOperationsFeatureView(baseURL: model.featureBaseURL, onOpenCodingSession: { handle, workspace in
                                Task { if await model.stageCodingSession(handle, workspace: workspace) { selection = .coding } }
                            })
                        case .devices:
                            TabView {
                                NativeConnectionsFeatureView(baseURL: model.featureBaseURL, sessionID: model.activeConversationID.isEmpty ? nil : model.activeConversationID).tabItem { Label("Connections", systemImage: "network") }
                                NativeHardwareFeatureView(baseURL: model.featureBaseURL).tabItem { Label("Device methods", systemImage: "sensor") }
                            }
                        case .security:
                            TabView {
                                NativeVaultFeatureView(baseURL: model.securityBaseURL).tabItem { Label("Credential vault", systemImage: "lock.shield") }
                                NativeSecurityFeatureView(baseURL: model.securityBaseURL).tabItem { Label("Permissions and cost", systemImage: "checkmark.shield") }
                            }
                        case .settings: NativeSettingsView(model: model, desktop: desktop)
                        }
                    }.frame(maxWidth: .infinity, maxHeight: .infinity)
                }.navigationSplitViewStyle(.balanced)
                .task(id: selection) { await refreshSelected() }
                .onChange(of: model.ready) { ready in
                    if ready { Task { await refreshSelected() } }
                }
            }
        }.tint(nativeAccent)
        .frame(minWidth: 760, minHeight: 600)
        .background(NativeDesktopWindowCapture(attach: host.attach))
        .onAppear {
            host.configure(model: model, desktop: desktop, reopen: { openWindow(id: "feral-main") })
            consumeDesktopIntent()
        }
        .onChange(of: selection) { value in if let value { desktop.rememberDestination(value.rawValue) } }
        .onChange(of: host.pendingIntent?.id) { _ in consumeDesktopIntent() }
        .onChange(of: model.onboarded) { _ in host.refreshState(); consumeDesktopIntent() }
        .onChange(of: model.ready) { _ in host.refreshState() }
        .onChange(of: model.busy) { _ in host.refreshState() }
        .onReceive(NotificationCenter.default.publisher(for: .feralShowNavigation)) { _ in
            guard model.onboarded else { return }
            navigationQuery = ""; navigationPresented = true
        }
        .onReceive(NotificationCenter.default.publisher(for: .feralNavigate)) { notification in
            guard model.onboarded, let destination = notification.object as? NativeDestination else { return }
            selection = destination
        }
        .sheet(isPresented: $navigationPresented) {
            VStack(alignment: .leading, spacing: 16) {
                HStack { Text("Go to…").font(.title2.weight(.semibold)); Spacer(); Button("Done") { navigationPresented = false }.keyboardShortcut(.escape, modifiers: []) }
                TextField("Search Chat, Memory, Coding…", text: $navigationQuery)
                    .textFieldStyle(.roundedBorder).focused($navigationFocused)
                    .onSubmit { if let first = matchingDestinations.first { selectDestination(first) } }
                    .accessibilityLabel("Search destinations")
                ScrollView {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(matchingDestinations) { destination in
                            Button { selectDestination(destination) } label: {
                                HStack { Label(destination.rawValue, systemImage: destination.icon); Spacer(); if destination == selection { Image(systemName: "checkmark").accessibilityLabel("Current destination") } }
                                    .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                            }.buttonStyle(.plain)
                        }
                        if matchingDestinations.isEmpty { Text("No matching destinations").foregroundStyle(.secondary).padding(10) }
                    }
                }
                Text("Return opens the first result. ⌘K opens this search.").font(.caption).foregroundStyle(.secondary)
            }.padding(24).frame(width: 460, height: 480).onAppear { navigationFocused = true }
        }
    }

    private func consumeDesktopIntent() {
        guard model.onboarded, let intent = host.pendingIntent else { return }
        switch intent.action {
        case .destination(let destination): selection = destination; navigationPresented = false
        case .palette: navigationQuery = ""; navigationPresented = true
        }
        host.consumeIntent(intent.id)
    }

    private func selectDestination(_ destination: NativeDestination) { selection = destination; navigationPresented = false }

    private func navigationRows(_ destinations: [NativeDestination]) -> some View {
        ForEach(destinations) { item in Label(item.rawValue, systemImage: item.icon).padding(.vertical, 5).tag(item) }
    }

    private func refreshSelected() async {
        guard model.ready else { return }
        switch selection ?? .chat {
        case .coding: await model.refreshCoding()
        default: break
        }
    }
}

private struct NativeBrandMark: View {
    var size: CGFloat = 32
    var body: some View {
        Group {
            if let url = Bundle.main.url(forResource: "FeralLogo", withExtension: "png"),
               let image = NSImage(contentsOf: url) {
                Image(nsImage: image).resizable().scaledToFit()
            } else {
                Text("F").font(.headline)
            }
        }.frame(width: size, height: size)
            .background(Color.white).clipShape(RoundedRectangle(cornerRadius: size * 0.22))
            .accessibilityLabel("FERAL logo")
    }
}

private struct NativeAvatar: View {
    let choice: String
    let importedPath: String?
    var size: CGFloat = 64

    private var portrait: NSImage? {
        if choice == "imported", let path = importedPath { return NSImage(contentsOfFile: path) }
        guard choice == "photo", let url = Bundle.main.url(forResource: "FeralAvatar", withExtension: "png") else { return nil }
        return NSImage(contentsOf: url)
    }

    var body: some View {
        Group {
            if let portrait {
                Image(nsImage: portrait).resizable().scaledToFill()
            } else {
                ZStack {
                    Circle().fill(RadialGradient(colors: [.white.opacity(0.95), Color(red: 0.89, green: 0.72, blue: 0.64), nativeAccent], center: .init(x: 0.32, y: 0.27), startRadius: 0, endRadius: size * 0.8))
                    Circle().strokeBorder(.white.opacity(0.45), lineWidth: 1)
                }
            }
        }.frame(width: size, height: size).clipShape(Circle())
            .overlay(Circle().strokeBorder(Color.primary.opacity(0.08), lineWidth: 1))
            .accessibilityLabel(choice == "orb" ? "FERAL orb avatar" : "FERAL portrait avatar")
    }
}

private struct NativeAvatarPicker: View {
    @ObservedObject var model: NativeModel
    @State private var importPresented = false
    var compact = false

    var body: some View {
        HStack(spacing: compact ? 18 : 24) {
            avatarButton("photo", label: "FERAL", detail: "Your companion")
            avatarButton("orb", label: "Orb", detail: "A quiet presence")
            Button { importPresented = true } label: {
                VStack(spacing: 10) {
                    if model.avatarChoice == "imported" {
                        NativeAvatar(choice: "imported", importedPath: model.importedAvatarPath, size: compact ? 62 : 100)
                    } else {
                        Image(systemName: "person.crop.circle.badge.plus").font(.system(size: compact ? 34 : 44, weight: .light))
                            .foregroundStyle(.secondary).frame(width: compact ? 62 : 100, height: compact ? 62 : 100)
                    }
                    Text("Your photo").font(.headline)
                    if !compact { Text("Import an image").font(.caption).foregroundStyle(.secondary) }
                }.frame(width: compact ? 108 : 150).padding(.vertical, compact ? 14 : 22)
                    .background(RoundedRectangle(cornerRadius: 18).fill(model.avatarChoice == "imported" ? nativeAccent.opacity(0.09) : Color(nsColor: .controlBackgroundColor)))
                    .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(model.avatarChoice == "imported" ? nativeAccent : Color.primary.opacity(0.08), lineWidth: 1))
            }.buttonStyle(.plain).accessibilityLabel("Import your avatar photo")
        }.fileImporter(isPresented: $importPresented, allowedContentTypes: [.image], allowsMultipleSelection: false) { result in
            switch result {
            case .success(let urls):
                if let url = urls.first { model.importAvatar(url) }
            case .failure(let error):
                if (error as NSError).code != NSUserCancelledError { model.error = "The photo could not be imported. \(error.localizedDescription)" }
            }
        }
    }

    private func avatarButton(_ choice: String, label: String, detail: String) -> some View {
        Button { model.avatarChoice = choice } label: {
            VStack(spacing: 10) {
                NativeAvatar(choice: choice, importedPath: nil, size: compact ? 62 : 100)
                Text(label).font(.headline)
                if !compact { Text(detail).font(.caption).foregroundStyle(.secondary) }
            }.frame(width: compact ? 108 : 150).padding(.vertical, compact ? 14 : 22)
                .background(RoundedRectangle(cornerRadius: 18).fill(model.avatarChoice == choice ? nativeAccent.opacity(0.09) : Color(nsColor: .controlBackgroundColor)))
                .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(model.avatarChoice == choice ? nativeAccent : Color.primary.opacity(0.08), lineWidth: 1))
        }.buttonStyle(.plain).accessibilityLabel("Choose \(label) avatar")
            .accessibilityAddTraits(model.avatarChoice == choice ? .isSelected : [])
    }
}

private struct NativeOnboardingView: View {
    @ObservedObject var model: NativeModel
    @State private var step = 0
    @State private var finishing = false
    @State private var providersPresented = false

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                NativeBrandMark(size: 28)
                Text("FERAL Native Preview").font(.headline)
                Spacer()
                Text(step == 0 ? "Make it yours" : "Connect your AI").font(.callout).foregroundStyle(.secondary)
            }.padding(28)
            Spacer(minLength: 24)
            VStack(spacing: 24) {
                if step == 0 {
                    VStack(spacing: 10) {
                        Text("Choose your companion").font(.system(size: 34, weight: .semibold, design: .rounded))
                        Text("A familiar face for every conversation.").font(.title3).foregroundStyle(.secondary)
                    }
                    NativeAvatarPicker(model: model)
                    VStack(alignment: .leading, spacing: 8) {
                        Text("What should FERAL call you?").font(.callout)
                        TextField("Your name (optional)", text: $model.displayName).textFieldStyle(.roundedBorder)
                    }.frame(maxWidth: 350)
                    if let error = model.error, !error.isEmpty { NativeErrorBanner(text: error).frame(maxWidth: 500) }
                    Button("Continue") { step = 1; Task { await model.start() } }.buttonStyle(.borderedProminent).controlSize(.large)
                        .keyboardShortcut(.defaultAction)
                } else {
                    NativeAvatar(choice: model.avatarChoice, importedPath: model.importedAvatarPath, size: 76)
                    VStack(spacing: 10) {
                        Text("Give FERAL a place to think").font(.system(size: 30, weight: .semibold, design: .rounded))
                        Text("Connect a local AI service, choose another provider, or set up AI later.")
                            .font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    }
                    NativeAIFields(model: model).frame(maxWidth: 460)
                    Button("Choose another AI provider") { providersPresented = true }.disabled(!model.ready)
                    if let error = model.error, !error.isEmpty { NativeErrorBanner(text: error).frame(maxWidth: 500) }
                    if !model.ready {
                        NativeStartupStatus(model: model)
                    }
                    HStack(spacing: 14) {
                        Button("Back") { step = 0 }.controlSize(.large)
                        Button(finishing ? "Connecting…" : "Start chatting") {
                            finishing = true
                            Task { await model.finishOnboarding(); finishing = false }
                        }.buttonStyle(.borderedProminent).controlSize(.large)
                            .disabled(!model.ready || finishing || model.endpoint.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.modelName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                            .keyboardShortcut(.defaultAction)
                    }
                    Button("Set up AI later") { model.completeProfileOnboarding() }.disabled(!model.ready || finishing)
                }
            }.padding(.horizontal, 32)
            Spacer(minLength: 24)
            Text("Native Preview · Some FERAL features are not available here yet.").font(.caption).foregroundStyle(.secondary).padding(24)
        }.frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color(nsColor: .windowBackgroundColor))
            .sheet(isPresented: $providersPresented) {
                VStack(spacing: 0) {
                    HStack { Text("AI Providers").font(.headline); Spacer(); Button("Done") { providersPresented = false } }.padding()
                    NativeProvidersFeatureView(baseURL: model.featureBaseURL, onConfigurationChanged: { Task { await model.refreshProviderConfiguration() } })
                    Button("Finish setup") { model.completeProfileOnboarding(); providersPresented = false }.padding().disabled(!model.ready)
                }.frame(width: 900, height: 650)
            }
    }
}

private struct NativeAIFields: View {
    @ObservedObject var model: NativeModel
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 6) {
                Text("Local AI address").font(.callout.weight(.medium))
                TextField("http://127.0.0.1:11434", text: $model.endpoint).textFieldStyle(.roundedBorder)
                    .accessibilityIdentifier("native-ai-endpoint")
            }
            VStack(alignment: .leading, spacing: 6) {
                Text("Model").font(.callout.weight(.medium))
                TextField("Model installed in your local AI service", text: $model.modelName).textFieldStyle(.roundedBorder)
                    .accessibilityIdentifier("native-ai-model")
            }
            Text("A running local service and an installed model are required.").font(.caption).foregroundStyle(.secondary)
        }
    }
}

private struct NativeChatView: View {
    @ObservedObject var model: NativeModel
    @State private var draft = ""
    @State private var attachmentPickerPresented = false
    @State private var dispatching = false
    @State private var attachmentReviewPresented = false
    @State private var chatToolsPresented = false
    @State private var attachmentReview: (draft: String, ids: [String], session: String)?
    @FocusState private var composerFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                NativePageHeader(title: "Chat", subtitle: "A conversation with your personal agent")
                Button("Conversation tools…") { chatToolsPresented = true }.disabled(!model.ready).padding(.trailing, 28)
            }
            DisclosureGroup("Conversation connection") {
                NativeSelectableText(text: model.recoveryStatus, font: .systemFont(ofSize: NSFont.smallSystemFontSize), color: .secondaryLabelColor).frame(maxWidth: .infinity, alignment: .leading)
            }.font(.caption).padding(.horizontal, 28).padding(.bottom, 10)
            if let error = model.chatError, !error.isEmpty { NativeErrorBanner(text: error) }
            if let error = model.attachmentError, !error.isEmpty { NativeErrorBanner(text: error) }
            if model.messages.isEmpty {
                Spacer()
                NativeAvatar(choice: model.avatarChoice, importedPath: model.importedAvatarPath, size: 104)
                Text(model.displayName.isEmpty ? "How can I help?" : "How can I help, \(model.displayName)?")
                    .font(.system(size: 28, weight: .semibold, design: .rounded)).padding(.top, 20)
                Text("Bring an idea, ask a question, or start something together.")
                    .foregroundStyle(.secondary).padding(.top, 4)
                Spacer()
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 24) {
                            ForEach(model.messages) { message in
                                HStack(alignment: .top, spacing: 12) {
                                    if message.role == "user" {
                                        Image(systemName: "person.crop.circle.fill").font(.system(size: 30)).foregroundStyle(.secondary)
                                    } else {
                                        NativeAvatar(choice: model.avatarChoice, importedPath: model.importedAvatarPath, size: 32)
                                    }
                                    VStack(alignment: .leading, spacing: 6) {
                                        Text(message.role == "user" ? (model.displayName.isEmpty ? "You" : model.displayName) : message.role == "assistant" ? "FERAL" : message.role.capitalized)
                                            .font(.callout.weight(.semibold)).foregroundStyle(.secondary)
                                        NativeRichMessageView(text: message.text, role: message.role, metadata: message.metadata)
                                        if let attachments = message.metadata["attachments"] as? [[String: Any]] {
                                            ForEach(Array(attachments.enumerated()), id: \.offset) { _, attachment in
                                                Label(attachment["filename"] as? String ?? "Attachment", systemImage: "paperclip").font(.caption).foregroundStyle(.secondary)
                                            }
                                        }
                                        if message.text.isEmpty && !message.metadata.isEmpty { Text("Structured message").foregroundStyle(.secondary) }
                                        if message.metadata["attachments"] != nil || !(message.metadata["content"] == nil || message.metadata["content"] is String) || (message.role != "user" && message.role != "assistant") {
                                            DisclosureGroup("Message details") {
                                                NativeSelectableText(text: NativeMemoryWire.json(message.metadata), font: .monospacedSystemFont(ofSize: NSFont.smallSystemFontSize, weight: .regular))
                                            }
                                        }
                                        if let failure = message.metadata["deliveryError"] as? String, !failure.isEmpty {
                                            HStack(alignment: .top) {
                                                Image(systemName: "exclamationmark.circle").foregroundStyle(Color.red)
                                                NativeSelectableText(text: message.role == "assistant" ? "Incomplete response: " + failure : failure, color: .systemRed)
                                            }.accessibilityLabel("Message delivery failed: \(failure)")
                                        }
                                    }
                                }.id(message.id)
                            }
                            if model.isSending { HStack(spacing: 9) { ProgressView().controlSize(.small); Text("Thinking…").foregroundStyle(.secondary) }.id("thinking") }
                            NativeChatEventsHost(model: model)
                        }.padding(.horizontal, 24).padding(.vertical, 20).frame(maxWidth: 850).frame(maxWidth: .infinity, alignment: .center)
                    }.frame(maxWidth: .infinity, maxHeight: .infinity).layoutPriority(1)
                        .onChange(of: model.messages.last?.text) { _ in
                            if let id = model.messages.last?.id { proxy.scrollTo(id, anchor: .bottom) }
                        }
                }
            }
            if model.messages.isEmpty {
                NativeEmptyChatEventsHost(model: model, rich: model.richChat)
            }
            if !model.pendingAttachments.isEmpty {
                ScrollView(.horizontal) {
                    HStack {
                        ForEach(model.pendingAttachments) { attachment in
                            HStack {
                                Label(attachment.filename, systemImage: "paperclip").lineLimit(1)
                                Button { model.pendingAttachments.removeAll { $0.id == attachment.id } } label: { Image(systemName: "xmark.circle.fill") }
                                    .buttonStyle(.plain).accessibilityLabel("Remove \(attachment.filename) from this message").disabled(dispatching || model.isSending)
                            }.font(.caption).padding(8).background(Color.secondary.opacity(0.08)).clipShape(RoundedRectangle(cornerRadius: 8))
                        }
                    }
                }.padding(.horizontal, 28)
                Text("Files are stored locally. Sending requires review: supported text can reach your configured model and fallbacks. PDFs, images and other binary formats are not yet readable here. Removing a chip does not delete the stored file.")
                    .font(.caption).foregroundStyle(.secondary).padding(.horizontal, 28)
            }
            HStack(alignment: .bottom, spacing: 12) {
                Button { attachmentPickerPresented = true } label: {
                    Image(systemName: "paperclip").font(.headline).frame(width: 28, height: 28)
                }.accessibilityLabel("Attach files").disabled(!model.ready || model.uploadingAttachments || model.isSending || model.switchingConversation || dispatching)
                if model.uploadingAttachments { ProgressView().controlSize(.small).accessibilityLabel("Uploading attachments") }
                TextEditor(text: $draft).font(.body).focused($composerFocused).scrollContentBackground(.hidden)
                    .frame(height: 64).padding(7)
                    .overlay(alignment: .topLeading) {
                        if draft.isEmpty { Text("Message FERAL…").foregroundStyle(.tertiary).padding(.top, 14).padding(.leading, 12).allowsHitTesting(false) }
                    }.accessibilityLabel("Message FERAL").accessibilityIdentifier("native-chat-composer")
                if model.isSending {
                    Button { Task { await model.stopChat() } } label: { Image(systemName: "stop.fill").font(.headline).frame(width: 28, height: 28) }
                        .buttonStyle(.bordered).clipShape(RoundedRectangle(cornerRadius: 10))
                        .keyboardShortcut(.escape, modifiers: []).accessibilityLabel("Stop response")
                } else {
                    Button(action: send) { Image(systemName: "arrow.up").font(.headline).frame(width: 28, height: 28) }
                        .buttonStyle(.borderedProminent).clipShape(RoundedRectangle(cornerRadius: 10))
                        .disabled(!model.ready || model.switchingConversation || model.uploadingAttachments || dispatching || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        .keyboardShortcut(.return, modifiers: .command).accessibilityLabel("Send message")
                }
            }.padding(12).background(RoundedRectangle(cornerRadius: 18).fill(Color(nsColor: .controlBackgroundColor)))
                .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(Color.primary.opacity(0.08), lineWidth: 1))
                .frame(maxWidth: 850).padding(.horizontal, 24).padding(.top, 10)
            Text("⌘ Return to send").font(.caption2).foregroundStyle(.tertiary).padding(.top, 7).padding(.bottom, 16)
        }.frame(maxWidth: .infinity, maxHeight: .infinity)
        .sheet(isPresented: $chatToolsPresented) {
            NativeChatToolsHost(model: model, voice: model.voice).frame(width: 820, height: 680)
        }.alert("Send attachment contents to the model?", isPresented: $attachmentReviewPresented) {
            Button("Send with attachments") {
                guard let review = attachmentReview, review.draft == draft,
                      review.session == model.activeConversationID,
                      review.ids == model.pendingAttachments.map(\.id) else {
                    model.attachmentError = "The message, thread or attachments changed. Review them again."
                    attachmentReview = nil; return
                }
                attachmentReview = nil
                dispatchSend(authorizedIDs: review.ids)
            }
            Button("Cancel", role: .cancel) { attachmentReview = nil }
        } message: {
            Text("Send the contents of \(model.pendingAttachments.map(\.filename).joined(separator: ", ")) to the configured chat model and any fallbacks. Cloud providers may receive this data. Readable content enters the conversation context and may inform its summaries. Supported UTF-8 text is limited to 64 KiB per message; other formats return an explicit unreadable status.")
        }.fileImporter(isPresented: $attachmentPickerPresented, allowedContentTypes: [.item], allowsMultipleSelection: true) { result in
            switch result {
            case .success(let urls): Task { await model.uploadAttachments(urls) }
            case .failure(let error): if (error as NSError).code != NSUserCancelledError { model.attachmentError = error.localizedDescription }
            }
        }
    }

    private func send() {
        if !model.pendingAttachments.isEmpty {
            attachmentReview = (draft, model.pendingAttachments.map(\.id), model.activeConversationID)
            attachmentReviewPresented = true
            return
        }
        dispatchSend(authorizedIDs: nil)
    }

    private func dispatchSend(authorizedIDs: [String]?) {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard model.ready, !model.isSending, !model.switchingConversation, !model.uploadingAttachments, !dispatching, !text.isEmpty else { return }
        let original = draft; dispatching = true
        Task {
            let sent = await model.sendChat(text, authorizedAttachmentIDs: authorizedIDs)
            if sent && draft == original { draft = "" }
            dispatching = false
        }
    }
}

private struct NativeChatEventsHost: View {
    @ObservedObject var model: NativeModel
    var body: some View {
        NativeRichChatEventsView(model: model.richChat, onPermissionResponse: { response in
            try await model.respondToChatPermission(response)
        })
    }
}

private struct NativeEmptyChatEventsHost: View {
    @ObservedObject var model: NativeModel
    @ObservedObject var rich: NativeRichChatModel
    var body: some View {
        if !rich.events.isEmpty || !rich.tools.isEmpty || !rich.permissions.isEmpty || !rich.reasoning.isEmpty || !rich.budgets.isEmpty || rich.plan != nil {
            ScrollView { NativeChatEventsHost(model: model).padding(.horizontal, 28) }.frame(maxHeight: 220)
        }
    }
}

private struct NativeChatToolsHost: View {
    @ObservedObject var model: NativeModel
    @ObservedObject var voice: NativeVoiceEngine
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack {
            HStack { Text("Conversation tools").font(.headline); Spacer(); Button("Done") { dismiss() } }.padding()
            NativeChatToolsFeatureView(baseURL: model.featureBaseURL, sessionID: model.activeConversationID.isEmpty ? nil : model.activeConversationID,
                                      isChatBusy: model.chatToolsHostBusy, todos: model.observedTodos,
                                      onOpenThread: { id, rows in try await model.applySnapshotHistory(id, history: rows) })
        }
    }
}

private struct NativeCodingView: View {
    @ObservedObject var model: NativeModel
    @State private var prompt = ""
    @State private var editingProvider = false

    private var running: Bool { ["running", "awaiting_permission"].contains(model.coding.status) }
    private var emptyCompletion: Bool { model.coding.status == "completed" && model.coding.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && model.coding.actions.isEmpty }
    var body: some View {
        VStack(spacing: 0) {
            NativePageHeader(title: "Coding", subtitle: "Build together in a project you choose")
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    nativeCard {
                        VStack(alignment: .leading, spacing: 14) {
                            HStack { Label("Project", systemImage: "folder").font(.headline); Spacer(); Button("Choose folder…", action: chooseFolder).disabled(!model.ready || running || model.codingBusy) }
                            NativeSelectableText(model.coding.workspace.isEmpty ? "Choose a project folder to begin." : model.coding.workspace)
                                .foregroundStyle(.secondary).lineLimit(3)
                                .id(model.coding.workspace)
                            Text("FERAL asks before making changes. Choosing a folder grants access to that project.").font(.caption).foregroundStyle(.secondary)
                        }.accessibilityElement(children: .contain)
                    }
                    if !model.coding.prepared || editingProvider {
                        nativeCard {
                            VStack(alignment: .leading, spacing: 14) {
                                Text("Coding AI").font(.headline)
                                Text("Coding uses its own local model connection.").font(.callout).foregroundStyle(.secondary)
                                TextField("Local AI address", text: $model.coding.endpoint).textFieldStyle(.roundedBorder).accessibilityLabel("Coding AI address")
                                TextField("Installed model name", text: $model.coding.modelName).textFieldStyle(.roundedBorder).accessibilityLabel("Coding model")
                                Button("Connect coding AI") { Task { if await model.prepareCoding() { editingProvider = false } } }
                                    .disabled(!model.ready || running || model.codingBusy || model.coding.endpoint.isEmpty || model.coding.modelName.isEmpty)
                            }
                        }
                    }
                    HStack(spacing: 8) {
                        Image(systemName: model.coding.engineReady && model.coding.prepared ? "checkmark.circle" : "info.circle")
                        Text(!model.coding.engineReady ? "Coding engine unavailable" : model.coding.prepared ? "Coding AI connected" : "Connect a model to start")
                        if model.coding.prepared && !editingProvider {
                            Button("Change coding AI…") { editingProvider = true }.disabled(running || model.codingBusy)
                        }
                        Spacer()
                        if !model.coding.status.isEmpty { Text(emptyCompletion ? "No result returned" : model.coding.status.replacingOccurrences(of: "_", with: " ").capitalized).foregroundStyle(.secondary) }
                    }.font(.callout)
                    if emptyCompletion {
                        Text("The coding agent ended without a reply or tool result. No edit or test result is verified. Try another task or change the coding AI.").font(.callout).foregroundStyle(.secondary)
                    }
                    VStack(alignment: .leading, spacing: 10) {
                        Text("What should we build or fix?").font(.headline)
                        NativePlainTextEditor(text: $prompt, label: "Coding task").frame(height: 95).padding(8)
                            .background(Color(nsColor: .textBackgroundColor)).clipShape(RoundedRectangle(cornerRadius: 10))
                            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.primary.opacity(0.12), lineWidth: 1))
                            .accessibilityLabel("Coding task").disabled(running || model.codingBusy)
                        HStack {
                            Button("Start task") { let request = prompt; Task { await model.startCoding(request) } }.buttonStyle(.borderedProminent)
                                .disabled(!model.ready || !model.coding.engineReady || !model.coding.prepared || model.coding.workspace.isEmpty || prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || running || model.codingBusy)
                            if running { Button("Stop task") { Task { await model.cancelCoding() } }.disabled(model.codingBusy) }
                            if model.codingBusy { ProgressView().controlSize(.small) }
                        }
                    }
                    ForEach(model.coding.permissions) { permission in
                        NativePermissionView(permission: permission, busy: model.codingBusy) { allowed in
                            Task { await model.answerPermission(permission.id, allowed) }
                        }
                    }
                    if !model.coding.text.isEmpty {
                        nativeCard { VStack(alignment: .leading, spacing: 10) { Text("Agent message").font(.headline); NativeSelectableText(model.coding.text) } }
                    }
                    if !model.coding.actions.isEmpty {
                        VStack(alignment: .leading, spacing: 12) {
                            Text("Observed actions").font(.headline)
                            ForEach(model.coding.actions) { action in
                                DisclosureGroup {
                                    NativeSelectableText(action.detail.isEmpty ? "No result detail was provided." : action.detail)
                                        .font(.system(.callout, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading).padding(.top, 8)
                                } label: {
                                    HStack { Text(action.title); Spacer(); Text(action.status).font(.caption).foregroundStyle(.secondary) }
                                }.padding(12).background(RoundedRectangle(cornerRadius: 10).fill(Color(nsColor: .controlBackgroundColor)))
                            }
                        }
                    }
                }.padding(28).frame(maxWidth: 900, alignment: .leading)
            }.frame(maxWidth: .infinity)
        }
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.title = "Choose a coding project"
        panel.message = "FERAL will have access to the folder you choose."
        panel.prompt = "Choose project"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.begin { response in
            guard response == .OK, let url = panel.url else { return }
            Task { await model.grantWorkspace(url.path) }
        }
    }
}

private struct NativePermissionView: View {
    let permission: NativePermission
    let busy: Bool
    let answer: (Bool) -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label("Your approval is needed", systemImage: "hand.raised").font(.headline)
            NativeSelectableText(permission.title)
            if !permission.before.isEmpty || !permission.after.isEmpty {
                HStack(alignment: .top, spacing: 12) {
                    codeColumn("Before", text: permission.before)
                    codeColumn("After", text: permission.after)
                }
            }
            if !permission.diff.isEmpty { codeColumn("Proposed change or command", text: permission.diff) }
            if !permission.reviewable {
                Text("The coding engine did not provide a concrete command or target. Approval is unavailable; deny this request and ask for a specific action.").font(.callout).foregroundStyle(.secondary)
            }
            HStack(spacing: 10) {
                Button("Allow this action") { answer(true) }.buttonStyle(.borderedProminent).disabled(busy || !permission.reviewable)
                Button("Deny") { answer(false) }.disabled(busy)
            }
        }.padding(20).background(RoundedRectangle(cornerRadius: 14).fill(Color.orange.opacity(0.07)))
            .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.orange.opacity(0.25), lineWidth: 1))
    }

    private func codeColumn(_ title: String, text: String) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).font(.caption.weight(.semibold))
            ScrollView([.horizontal, .vertical]) {
                NativeSelectableText(text.isEmpty ? "(empty file)" : text).font(.system(.callout, design: .monospaced)).frame(maxWidth: .infinity, alignment: .leading).padding(12)
            }.frame(minHeight: 80, maxHeight: 220).background(Color(nsColor: .textBackgroundColor)).clipShape(RoundedRectangle(cornerRadius: 8))
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct NativeMemoryView: View {
    @ObservedObject var model: NativeModel
    var body: some View {
        VStack(spacing: 0) {
            NativePageHeader(title: "Memory", subtitle: "What FERAL remembers", refresh: { Task { await model.refreshMemory() } })
            if model.memories.isEmpty {
                NativeEmptyView(icon: "brain", title: "Room to remember", detail: "No memories are available yet. Memories will appear here as you use FERAL.")
            } else {
                List(model.memories) { item in VStack(alignment: .leading, spacing: 7) { Text(item.title).font(.headline); NativeSelectableText(item.detail).foregroundStyle(.secondary) }.padding(.vertical, 10) }.listStyle(.inset)
            }
        }
    }
}

private struct NativeDevicesView: View {
    @ObservedObject var model: NativeModel
    var body: some View {
        VStack(spacing: 0) {
            NativePageHeader(title: "Devices", subtitle: "Your connected companions", refresh: { Task { await model.refreshDevices() } })
            if model.devices.isEmpty {
                NativeEmptyView(icon: "eyeglasses", title: "Your devices belong here", detail: "No devices are connected. Paired glasses and other devices will appear when the connection is available.")
            } else {
                List(model.devices) { item in HStack(alignment: .top, spacing: 14) { Image(systemName: "network").font(.title2).foregroundStyle(nativeAccent); VStack(alignment: .leading, spacing: 7) { Text(item.title).font(.headline); NativeSelectableText(item.detail).foregroundStyle(.secondary) } }.padding(.vertical, 12) }.listStyle(.inset)
            }
        }
    }
}

private struct NativeSettingsView: View {
    @ObservedObject var model: NativeModel
    @ObservedObject var desktop: NativeDesktopExperience
    @State private var saving = false
    @State private var speechSettingsPresented = false
    @State private var integrationsPresented = false
    var body: some View {
        VStack(spacing: 0) {
            NativePageHeader(title: "Settings", subtitle: "Make FERAL feel like yours")
            ScrollView {
                VStack(alignment: .leading, spacing: 26) {
                    VStack(alignment: .leading, spacing: 14) { Text("Your companion").font(.headline); NativeAvatarPicker(model: model, compact: true) }
                    VStack(alignment: .leading, spacing: 8) { Text("Your name").font(.headline); TextField("Your name (optional)", text: $model.displayName).textFieldStyle(.roundedBorder) }
                    Button("Save profile") { model.saveProfile() }.buttonStyle(.borderedProminent)
                    Button("Continue provider setup…") { model.openProviderSetup() }
                Text("Manage models and credentials in AI Providers.").font(.caption).foregroundStyle(.secondary)
                    HStack {
                        Button("Speech settings…") { speechSettingsPresented = true }
                        Button("Accounts, channels and tools…") { integrationsPresented = true }
                    }.disabled(!model.ready)
                    Divider()
                    NativeDesktopExperienceView(experience: desktop)
                    Divider()
                    NativeConfigurationFeatureView(baseURL: model.featureBaseURL)
                }.padding(28).frame(maxWidth: 650, alignment: .leading)
            }.frame(maxWidth: .infinity, alignment: .leading)
        }.sheet(isPresented: $speechSettingsPresented) {
            VStack { HStack { Text("Speech settings").font(.headline); Spacer(); Button("Done") { speechSettingsPresented = false } }.padding(); NativeVoiceConfigurationFeatureView(baseURL: model.featureBaseURL) }.frame(width: 820, height: 680)
        }.sheet(isPresented: $integrationsPresented) {
            VStack { HStack { Text("Accounts, channels and tools").font(.headline); Spacer(); Button("Done") { integrationsPresented = false } }.padding(); NativeIntegrationFeatureView(baseURL: model.featureBaseURL) }.frame(width: 820, height: 680)
        }
    }
}

private struct NativePageHeader: View {
    let title: String
    let subtitle: String
    var refresh: (() -> Void)? = nil
    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 5) { Text(title).font(.title2.weight(.semibold)); Text(subtitle).font(.callout).foregroundStyle(.secondary) }
            Spacer()
            if let refresh { Button(action: refresh) { Image(systemName: "arrow.clockwise") }.accessibilityLabel("Refresh \(title.lowercased())") }
        }.padding(.horizontal, 28).padding(.vertical, 20)
    }
}

private struct NativeErrorBanner: View {
    let text: String
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top) {
                Image(systemName: "exclamationmark.circle").accessibilityHidden(true)
                NativeSelectableText(NativeErrorPresentation.summary(text)).font(.callout)
                    .foregroundStyle(.red).lineLimit(2)
                    .accessibilityLabel("Error: " + NativeErrorPresentation.summary(text))
            }.accessibilityElement(children: .contain)
            DisclosureGroup("Error details") {
                ScrollView([.horizontal, .vertical]) {
                    NativeSelectableText(verbatim: text).font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .accessibilityLabel("Exact error details: " + text)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }.frame(height: 80)
            }.font(.caption).foregroundStyle(.secondary)
        }.font(.callout).foregroundStyle(Color.red).padding(10)
            .frame(maxWidth: .infinity, alignment: .leading).background(Color.red.opacity(0.06))
            .accessibilityElement(children: .contain)
    }
}

private struct NativeStartupStatus: View {
    @ObservedObject var model: NativeModel
    var openSecurity: (() -> Void)? = nil
    var body: some View {
        HStack(spacing: 10) {
            if model.busy { ProgressView().controlSize(.small) }
            Text(model.startupStatus).foregroundStyle(.secondary)
            if model.serviceReachable && !model.ready, let openSecurity {
                Button("Open Security", action: openSecurity)
            } else if !model.busy && model.error != nil {
                Button("Retry") { Task { await model.start() } }.accessibilityLabel("Retry starting FERAL")
            }
        }.font(.callout)
    }
}

private struct NativeEmptyView: View {
    let icon: String
    let title: String
    let detail: String
    var body: some View {
        VStack(spacing: 14) { Image(systemName: icon).font(.system(size: 44, weight: .light)).foregroundStyle(.secondary); Text(title).font(.title2.weight(.medium)); Text(detail).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 430) }
            .padding(30).frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private func nativeCard<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
    content().frame(maxWidth: .infinity, alignment: .leading).padding(18)
        .background(RoundedRectangle(cornerRadius: 14).fill(Color(nsColor: .controlBackgroundColor)))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.primary.opacity(0.07), lineWidth: 1))
}
