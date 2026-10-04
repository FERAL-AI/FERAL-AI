import Foundation
import CoreFoundation
import SwiftUI
import AppKit
import ImageIO

struct NativeBrowserViewFailure: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}
struct NativeBrowserViewTarget: Identifiable, Equatable { let id: String, label: String }
struct NativeBrowserViewLease {
    let id: String, token: String, target: String, expires: Date
}
struct NativeBrowserViewCursor { let x: Double, y: Double, phase: String }
struct NativeBrowserViewFrame {
    let sequence: Int64, captured: Date, image: NSImage, width: Int, height: Int
    let cursor: NativeBrowserViewCursor?
}
enum NativeBrowserViewWire {
    static func bool(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }
    static func number(_ value: Any?) -> Double? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue.isFinite else { return nil }
        return number.doubleValue
    }
    static func integer(_ value: Any?, maximum: Int64 = Int64.max - 1) -> Int64? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              !["f", "d"].contains(String(cString: number.objCType)),
              let integer = Int64(number.stringValue), integer >= 0, integer <= maximum else { return nil }
        return integer
    }
    static func identifier(_ value: Any?, limit: Int = 256) -> String? {
        guard let text = value as? String, !text.isEmpty, text.utf8.count <= limit,
              text.unicodeScalars.allSatisfy({ CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.").contains($0) }) else { return nil }
        return text
    }
    static func targets(_ value: [String: Any]) throws -> ([NativeBrowserViewTarget], String?) {
        guard bool(value["success"]) == true, let connected = bool(value["connected"]),
              let rows = value["targets"] as? [[String: Any]], rows.count <= 1 else { throw NativeBrowserViewFailure(message: "Browser inventory could not be verified.") }
        var targets: [NativeBrowserViewTarget] = []
        for row in rows {
            guard let id = identifier(row["id"]), let label = row["label"] as? String,
                  !label.isEmpty, label.utf8.count <= 1024, !label.unicodeScalars.contains(where: { $0.value < 32 || $0.value == 127 }) else { throw NativeBrowserViewFailure(message: "Browser target identity could not be verified.") }
            targets.append(.init(id: id, label: label))
        }
        guard Set(targets.map(\.id)).count == targets.count else { throw NativeBrowserViewFailure(message: "Browser inventory contains duplicate targets.") }
        let active = identifier(value["active_target_id"])
        guard connected ? (active != nil && targets.contains(where: { $0.id == active })) : (targets.isEmpty && active == nil) else { throw NativeBrowserViewFailure(message: "The attached browser target could not be verified.") }
        return (targets, active)
    }
    static func lease(_ value: [String: Any], target: String, now: Date) throws -> NativeBrowserViewLease {
        guard bool(value["success"]) == true, let id = identifier(value["view_id"]),
              UUID(uuidString: id)?.uuidString.lowercased() == id,
              let token = identifier(value["token"], limit: 43), token.count == 43, !token.contains("."), value["target_id"] as? String == target,
              let expiry = number(value["expires_at"]), expiry > now.timeIntervalSince1970,
              expiry - now.timeIntervalSince1970 <= 300 else { throw NativeBrowserViewFailure(message: "The browser viewing grant could not be verified. No frame is displayed.") }
        return .init(id: id, token: token, target: target, expires: Date(timeIntervalSince1970: expiry))
    }
    static func frame(_ value: [String: Any], lease: NativeBrowserViewLease, previous: Int64?, now: Date) throws -> NativeBrowserViewFrame {
        guard bool(value["success"]) == true, bool(value["masked_password_fields"]) == true, value["view_id"] as? String == lease.id,
              value["target_id"] as? String == lease.target, now < lease.expires,
              let sequence = integer(value["sequence"]), previous == nil || sequence > previous!,
              let captured = number(value["captured_at"]), captured <= now.timeIntervalSince1970 + 2,
              captured >= now.timeIntervalSince1970 - 10, value["format"] as? String == "jpeg",
              let width = integer(value["width"], maximum: 1280), width > 0,
              let height = integer(value["height"], maximum: 1280), height > 0,
              let encoded = value["image_b64"] as? String, encoded.utf8.count <= 2_097_152,
              let data = Data(base64Encoded: encoded), !data.isEmpty, data.count <= 2_097_152,
              let source = CGImageSourceCreateWithData(data as CFData, nil),
              CGImageSourceGetType(source) as String? == "public.jpeg", CGImageSourceGetCount(source) == 1,
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              integer(properties[kCGImagePropertyPixelWidth]) == width,
              integer(properties[kCGImagePropertyPixelHeight]) == height,
              let image = NSImage(data: data), image.isValid else { throw NativeBrowserViewFailure(message: "The browser frame is expired, mismatched or unreadable. Viewing stopped.") }
        var cursor: NativeBrowserViewCursor?
        if let raw = value["cursor"] as? [String: Any] {
            guard let x = number(raw["x"]), let y = number(raw["y"]), x >= 0, y >= 0,
                  x < Double(width), y < Double(height) else { throw NativeBrowserViewFailure(message: "Browser input location is invalid. Viewing stopped.") }
            let phase = raw["phase"] as? String ?? "move"
            guard (raw["phase"] == nil || raw["phase"] is NSNull || raw["phase"] is String),
                  ["move", "hover", "click"].contains(phase) else { throw NativeBrowserViewFailure(message: "Browser input location is invalid. Viewing stopped.") }
            cursor = .init(x: x, y: y, phase: phase)
        } else if value["cursor"] != nil && !(value["cursor"] is NSNull) { throw NativeBrowserViewFailure(message: "Browser input location is malformed.") }
        return .init(sequence: sequence, captured: Date(timeIntervalSince1970: captured), image: image, width: Int(width), height: Int(height), cursor: cursor)
    }
}
private final class BrowserViewRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}
@MainActor final class NativeBrowserViewModel: ObservableObject {
    @Published private(set) var targets: [NativeBrowserViewTarget] = []
    @Published private(set) var activeTarget: String?
    @Published var selectedTarget = ""
    @Published private(set) var frame: NativeBrowserViewFrame?
    @Published private(set) var watching = false
    @Published private(set) var busy = false
    @Published private(set) var status = "Refresh the browser inventory to begin."
    @Published private(set) var error: String?
    private var baseURL: URL?
    private var generation = UUID()
    private var lease: NativeBrowserViewLease?
    private var poll: Task<Void, Never>?
    private var expiry: Task<Void, Never>?
    private let session: URLSession
    private let now: () -> Date
    private let automaticPolling: Bool
    private var framePending = false
    init(session: URLSession? = nil, now: @escaping () -> Date = Date.init, automaticPolling: Bool = true) {
        self.session = session ?? URLSession(configuration: .ephemeral, delegate: BrowserViewRedirectGuard(), delegateQueue: nil)
        self.now = now; self.automaticPolling = automaticPolling
    }
    var canStart: Bool { !busy && !watching && activeTarget != nil && selectedTarget == activeTarget }
    func configure(baseURL: URL?) async {
        guard self.baseURL != baseURL else { return }
        let current = UUID(); await retire(current)
        guard generation == current else { return }
        self.baseURL = baseURL
        targets = []; activeTarget = nil; selectedTarget = ""; frame = nil; error = nil
        await refresh()
    }
    private func request(_ path: String, origin: URL, body: [String: Any]? = nil) async throws -> [String: Any] {
        guard ["127.0.0.1", "::1", "[::1]"].contains(origin.host ?? ""), ["http", "https"].contains(origin.scheme ?? ""),
              origin.user == nil, origin.password == nil, var parts = URLComponents(url: origin, resolvingAgainstBaseURL: false) else { throw NativeBrowserViewFailure(message: "Connect to the app-owned local agent.") }
        parts.percentEncodedPath = path; parts.queryItems = nil; parts.fragment = nil
        guard let url = parts.url else { throw NativeBrowserViewFailure(message: "The local browser-view address is invalid.") }
        var request = URLRequest(url: url); request.timeoutInterval = 10
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("native-v1", forHTTPHeaderField: "X-FERAL-Browser-View")
        if let body { request.httpMethod = "POST"; request.httpBody = try JSONSerialization.data(withJSONObject: body); request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        let (data, response) = try await session.feralLocalData(for: request)
        guard let http = response as? HTTPURLResponse, data.count <= 3_000_000,
              let value = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw NativeBrowserViewFailure(message: "The browser-view response could not be read.") }
        guard (200..<300).contains(http.statusCode), NativeBrowserViewWire.bool(value["success"]) == true else {
            let code = value["error_code"] as? String ?? ""
            let message: String
            switch code {
            case "browser_not_connected", "no_browser", "view_unavailable": message = "No browser is attached. Open the browser agent separately, then refresh."
            case "privacy_refused", "consent_required", "view_consent_required", "view_not_authorized": message = "Browser viewing was not permitted. Review viewing consent before starting."
            case "view_mask_failed": message = "The browser page could not be masked safely. No image is displayed."
            case "target_changed", "target_not_active", "view_target_changed": message = "The attached browser page changed. Refresh and choose its current target."
            case "view_expired", "view_not_found", "view_expired_or_changed": message = "The browser viewing grant ended. Start a new reviewed view."
            default: message = "The browser could not confirm this viewing operation. Refresh before trying again."
            }
            throw NativeBrowserViewFailure(message: message)
        }
        return value
    }
    func refresh() async {
        guard !watching && !busy, let origin = baseURL else { return }
        let current = generation; busy = true; defer { if generation == current { busy = false } }
        do {
            let body = try await request("/api/browser/view/targets", origin: origin)
            guard generation == current else { return }
            let inventory = try NativeBrowserViewWire.targets(body)
            targets = inventory.0; activeTarget = inventory.1; selectedTarget = inventory.1 ?? ""; error = nil
            status = activeTarget == nil ? "No browser is attached. Viewing does not launch one." : "Ready to review the attached browser page."
        } catch { if generation == current { targets = []; activeTarget = nil; selectedTarget = ""; self.error = error.localizedDescription } }
    }
    func start() async {
        guard canStart, let origin = baseURL else { return }
        let current = generation, target = selectedTarget; busy = true; error = nil
        defer { if generation == current { busy = false } }
        do {
            let body = try await request("/api/browser/view/start", origin: origin, body: ["target_id": target, "consent": true])
            let granted = try NativeBrowserViewWire.lease(body, target: target, now: now())
            guard generation == current else { await revoke(granted, origin: origin); return }
            lease = granted; watching = true; status = "Viewing the attached browser page."
            let lifetime = min(300, max(0, granted.expires.timeIntervalSince(now())))
            expiry = Task { [weak self] in
                do { try await Task.sleep(nanoseconds: UInt64(lifetime * 1_000_000_000)) } catch { return }
                guard let self, self.generation == current, self.watching else { return }
                let retirement = UUID(); await self.retire(retirement)
                if self.generation == retirement { self.error = "The browser viewing grant expired." }
            }
            if automaticPolling {
                poll = Task { [weak self] in
                    while !Task.isCancelled {
                        guard let self, self.generation == current, self.watching else { return }
                        await self.pollFrame()
                        guard self.generation == current, self.watching else { return }
                        do { try await Task.sleep(nanoseconds: 500_000_000) } catch { return }
                    }
                }
            }
        } catch { if generation == current { self.error = error.localizedDescription + " Start was not confirmed; no image is displayed."; activeTarget = nil; status = "Viewing unavailable." } }
    }
    func pollFrame() async {
        guard watching, !framePending, let lease, let origin = baseURL else { return }
        let current = generation; framePending = true
        defer { if generation == current { framePending = false } }
        do {
            guard now() < lease.expires else { throw NativeBrowserViewFailure(message: "The browser viewing grant expired.") }
            let body = try await request("/api/browser/view/frame", origin: origin, body: ["view_id": lease.id, "token": lease.token])
            guard generation == current, watching, self.lease?.id == lease.id else { return }
            frame = try NativeBrowserViewWire.frame(body, lease: lease, previous: frame?.sequence, now: now())
            error = nil
        } catch {
            guard generation == current else { return }
            let failure = error.localizedDescription, retirement = UUID(); await retire(retirement)
            if generation == retirement { self.error = failure; status = "Viewing stopped." }
        }
    }
    func stop() async {
        await retire(UUID())
    }
    private func retire(_ current: UUID) async {
        let held = lease, origin = baseURL
        generation = current
        poll?.cancel(); poll = nil; expiry?.cancel(); expiry = nil; lease = nil; frame = nil; watching = false; busy = false; framePending = false
        status = "Viewing stopped."
        guard let held, let origin else { return }
        // The polling task may have cancelled itself. Revocation must still be
        // admitted on an independent task; local media is already retired.
        let cleanup = Task { await self.revoke(held, origin: origin) }
        let confirmed = await cleanup.value
        if generation == current { error = confirmed ? nil : "Local viewing stopped; remote revocation was not confirmed. The grant expires automatically." }
    }
    @discardableResult private func revoke(_ held: NativeBrowserViewLease, origin: URL) async -> Bool {
        do {
            let result = try await request("/api/browser/view/stop", origin: origin, body: ["view_id": held.id, "token": held.token])
            return NativeBrowserViewWire.bool(result["success"]) == true
        } catch { return false }
    }
}
struct NativeBrowserViewFeatureView: View {
    let baseURL: URL?
    @StateObject private var model = NativeBrowserViewModel()
    @State private var review = false
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack { Text("Browser").font(.title2.bold()); Spacer(); Button("Refresh") { Task { await model.refresh() } }.disabled(model.busy || model.watching) }
            Text("Watch FERAL work in the attached browser tab.").foregroundStyle(.secondary)
            HStack {
                Picker("Browser page", selection: $model.selectedTarget) {
                    Text("Choose the attached page").tag("")
                    ForEach(model.targets) { target in Text(target.label).tag(target.id).disabled(target.id != model.activeTarget) }
                }.disabled(model.busy || model.watching)
                if model.watching { Button("Stop viewing") { Task { await model.stop() } } }
                else { Button("Start viewing…") { review = true }.disabled(!model.canStart) }
            }
            if let frame = model.frame {
                GeometryReader { geometry in
                    let scale = min(geometry.size.width / CGFloat(frame.width), geometry.size.height / CGFloat(frame.height))
                    let width = CGFloat(frame.width) * scale, height = CGFloat(frame.height) * scale
                    ZStack(alignment: .topLeading) {
                        Image(nsImage: frame.image).resizable().interpolation(.medium).frame(width: width, height: height)
                        if let cursor = frame.cursor { Circle().stroke(.orange, lineWidth: 3).background(Circle().fill(.orange.opacity(0.2))).frame(width: 16, height: 16).offset(x: cursor.x * scale - 8, y: cursor.y * scale - 8).accessibilityLabel("Browser input location") }
                    }.frame(width: geometry.size.width, height: geometry.size.height, alignment: .center)
                }.background(.black.opacity(0.1)).accessibilityLabel("Live browser page")
                Text("Frame \(frame.sequence) · \(frame.captured.formatted(date: .omitted, time: .standard))\(frame.cursor == nil ? "" : " · Browser input location")").font(.caption).foregroundStyle(.secondary)
            } else {
                VStack(spacing: 8) { Image(systemName: "rectangle.on.rectangle").font(.largeTitle); Text(model.watching ? "Waiting for the browser…" : model.activeTarget == nil ? "Start a browser task in Chat, then refresh here." : "Start viewing to see this browser tab.") }.frame(maxWidth: .infinity, maxHeight: .infinity).foregroundStyle(.secondary)
            }
            Text(model.status).font(.callout)
            if let error = model.error { NativeSelectableText(error).foregroundStyle(.red) }
            Text("Viewing this browser tab does not stop or authorize agent tasks. Use Chat or Oversight for task Stop.").font(.caption).foregroundStyle(.secondary)
        }.padding(24)
        .task(id: baseURL?.absoluteString ?? "") { review = false; await model.configure(baseURL: baseURL) }
        .onDisappear { review = false; Task { await model.stop() } }
        .alert("View the attached browser page?", isPresented: $review) {
            Button("Cancel", role: .cancel) { }
            Button("Start viewing") { Task { await model.start() } }
        } message: {
            Text("The currently attached page's visible content will appear here. Password fields are masked and embedded frames are hidden. Unsupported pages may stop viewing. Other personal or account content may be visible. Frames remain in memory and are not saved by this view. Viewing does not authorize actions on this page.")
        }
    }
}
