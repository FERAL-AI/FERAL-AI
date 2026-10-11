import Foundation
import Combine
import CoreFoundation

// Read-only snapshots. A digest identifies changed presentation, not an event,
// an exactly-once action, or evidence that an external effect succeeded.
struct NativeTaskReceipt: Identifiable, Equatable {
    let id: String, sessionID: String, title: String, status: String, processing: String
    let currentStep: Int, total: Int, completed: Int
    let resultPresent: Bool, result: String?, truncated: Bool, resultStep: Int?, errorCode: String?
    let digest: String
    let createdAt: Double, updatedAt: Double
    var label: String {
        switch processing {
        case "in_progress": return status == "queued" ? "Queued" : "Processing"
        case "awaiting_approval": return "Waiting for approval"
        case "outcome_unknown": return "Outcome needs checking"
        case "completed": return "Processing completed"
        case "failed": return "Processing failed"
        case "cancelled": return "Processing cancelled"
        default: return "Waiting"
        }
    }
}

enum NativeTaskReceiptWire {
    static let keys: Set<String> = ["contract_version", "flow_id", "origin_session_id", "title", "status", "current_step", "steps_total", "steps_completed", "processing_outcome", "action_outcome", "result_present", "result_text", "result_truncated", "result_step_index", "error_code", "result_digest", "created_at", "updated_at"]
    static func boolean(_ value: Any?) -> Bool? {
        guard let n = value as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() else { return nil }; return n.boolValue
    }
    static func integer(_ value: Any?) -> Int? {
        guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), ["i", "s", "l", "q", "I", "S", "L", "Q"].contains(String(cString: n.objCType)), n.doubleValue.isFinite, n.doubleValue >= 0, n.doubleValue <= Double(Int.max - 1024) else { return nil }; return n.intValue
    }
    static func time(_ value: Any?) -> Double? {
        guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue.isFinite, (0...253402300799).contains(n.doubleValue) else { return nil }; return n.doubleValue
    }
    static func boundedID(_ value: Any?, max: Int = 128) -> String? {
        guard let s = value as? String, !s.isEmpty, s.unicodeScalars.count <= max, !s.unicodeScalars.contains(where: { $0.value < 32 || $0.value == 127 }) else { return nil }; return s
    }
    static func cursor(_ value: Any?) -> [String: Any]? {
        guard let row = value as? [String: Any], Set(row.keys) == ["created_at", "flow_id"], let t = time(row["created_at"]), let id = boundedID(row["flow_id"]) else { return nil }; return ["created_at": t, "flow_id": id]
    }
    static func receipt(_ value: Any?, sessionID: String) -> NativeTaskReceipt? {
        guard let row = value as? [String: Any], Set(row.keys) == keys,
              integer(row["contract_version"]) == 1, let id = boundedID(row["flow_id"]), row["origin_session_id"] as? String == sessionID,
              let title = row["title"] as? String, title.unicodeScalars.count <= 120,
              let status = row["status"] as? String, ["queued", "running", "waiting", "completed", "failed", "cancelled"].contains(status),
              let processing = row["processing_outcome"] as? String,
              processing == "outcome_unknown" || processing == "awaiting_approval" || processing == (["queued", "running"].contains(status) ? "in_progress" : status),
              row["action_outcome"] as? String == "not_asserted",
              let step = integer(row["current_step"]), let total = integer(row["steps_total"]), (1...32).contains(total), step <= total,
              let completed = integer(row["steps_completed"]), completed <= total,
              let present = boolean(row["result_present"]), let truncated = boolean(row["result_truncated"]),
              let digest = row["result_digest"] as? String, digest.utf8.count == 64, digest.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }),
              let created = time(row["created_at"]), let updated = time(row["updated_at"]), updated >= created else { return nil }
        let text = row["result_text"] as? String, index = integer(row["result_step_index"])
        if present {
            guard let text, text.utf8.count <= 4096, let index, index < total else { return nil }
        } else {
            guard row["result_text"] is NSNull, row["result_step_index"] is NSNull, !truncated else { return nil }
        }
        guard row["error_code"] is NSNull || boundedID(row["error_code"]) != nil else { return nil }
        return NativeTaskReceipt(id: id, sessionID: sessionID, title: title, status: status, processing: processing, currentStep: step, total: total, completed: completed, resultPresent: present, result: text, truncated: truncated, resultStep: index, errorCode: row["error_code"] as? String, digest: digest, createdAt: created, updatedAt: updated)
    }
}

@MainActor final class NativeTaskResultModel: ObservableObject {
    typealias Sender = ([String: Any]) async throws -> Void
    @Published private(set) var receipts: [NativeTaskReceipt] = []
    @Published private(set) var freshIDs = Set<String>()
    @Published private(set) var status = "Background task receipts are disconnected."
    @Published private(set) var historyLimited = false
    private(set) var sessionID: String?
    private(set) var connectionID: UUID?
    private(set) var supported = false
    private struct Pending { let id: String, flowID: String?, generation: UUID, sessionID: String, expires: Date }
    private var pending: Pending?
    private var pollTask: Task<Void, Never>?
    private var sendTask: Task<Void, Never>?
    private var sendID: String?
    private var sender: Sender?
    private var cursor: [String: Any]?
    private var more = false
    private var pages = 0
    private var lastDiscovery = Date.distantPast
    private var snapshotIndex = 0
    private let automaticPolling: Bool
    init(automaticPolling: Bool = true) { self.automaticPolling = automaticPolling }
    var requestID: String? { pending?.id }
    var retainedTasks: Int { (pollTask == nil ? 0 : 1) + (sendTask == nil ? 0 : 1) }

    func configure(sessionID: String?, connectionID: UUID?, sender: Sender? = nil) {
        pollTask?.cancel(); pollTask = nil; sendTask?.cancel()
        // Keep the cancelled sender retained until it settles. No new send may
        // overtake a still-running send, even when a transport ignores cancel.
        pending = nil; supported = false; freshIDs = []; self.sender = sender
        if self.sessionID != sessionID { receipts = []; cursor = nil; historyLimited = false; snapshotIndex = 0 }
        self.sessionID = sessionID; self.connectionID = connectionID
        more = false; pages = 0; lastDiscovery = .distantPast
        status = receipts.isEmpty ? "Background task receipts are disconnected." : "Reconnecting: saved task snapshots are not yet confirmed."
    }
    func negotiate(_ payload: [String: Any], connectionID: UUID) {
        guard self.connectionID == connectionID, let sessionID, payload["session_id"] as? String == sessionID else { return }
        guard let versions = payload["task_result_contract_versions"] as? [Any], versions.count <= 16,
              versions.allSatisfy({ NativeTaskReceiptWire.integer($0).map { (1...1024).contains($0) } ?? false }),
              versions.contains(where: { NativeTaskReceiptWire.integer($0) == 1 }), sender != nil else {
            supported = false; pending = nil; freshIDs = []; sendTask?.cancel(); pollTask?.cancel(); pollTask = nil; status = "This connection does not support background task receipts."; return
        }
        supported = true; status = "Checking recorded background tasks…"
        if automaticPolling, pollTask == nil {
            pollTask = Task { [weak self] in
                while !Task.isCancelled {
                    guard let self, self.connectionID == connectionID, self.supported else { return }
                    self.tick()
                    do { try await Task.sleep(nanoseconds: 2_000_000_000) } catch { return }
                }
            }
        }
    }
    func tick(now: Date = Date()) {
        guard supported, let sessionID, let generation = connectionID, let sender else { return }
        if let current = pending {
            if now < current.expires { return }
            pending = nil; freshIDs = []; status = "Task receipt check timed out. Recorded snapshots are unconfirmed."; sendTask?.cancel()
        }
        guard sendTask == nil else { return }
        let discover = (more && pages < 5) || now.timeIntervalSince(lastDiscovery) >= 30
        let flowID: String?
        var params: [String: Any] = ["contract_version": 1]
        if discover {
            if !more || pages >= 5 { pages = 0 }
            params["limit"] = 20; params["cursor"] = cursor ?? NSNull(); flowID = nil; lastDiscovery = now
        } else {
            guard !receipts.isEmpty else { return }
            flowID = receipts[snapshotIndex % receipts.count].id; snapshotIndex += 1; params["flow_id"] = flowID
        }
        let id = UUID().uuidString.lowercased()
        pending = Pending(id: id, flowID: flowID, generation: generation, sessionID: sessionID, expires: now.addingTimeInterval(8)); sendID = id
        let frame: [String: Any] = ["type": "req", "id": id, "method": discover ? "task.receipts" : "task.receipt", "params": params]
        sendTask = Task { [weak self] in
            do { try await sender(frame) }
            catch {
                guard let self, self.connectionID == generation, self.sessionID == sessionID, self.pending?.id == id else { self?.settleSend(id); return }
                self.pending = nil; self.freshIDs = []; self.status = "Task receipts could not be read. Recorded snapshots are unconfirmed."
            }
            self?.settleSend(id)
        }
    }
    private func settleSend(_ id: String) { if sendID == id { sendTask = nil; sendID = nil } }
    @discardableResult func consume(_ frame: [String: Any], connectionID: UUID, now: Date = Date()) -> Bool {
        guard frame["type"] as? String == "res", let id = frame["id"] as? String, let request = pending, request.id == id else { return false }
        guard self.connectionID == connectionID, request.generation == connectionID, self.sessionID == request.sessionID else { return true }
        pending = nil
        guard now < request.expires, supported,
              frame["session_id"] as? String == nil || frame["session_id"] as? String == request.sessionID,
              NativeTaskReceiptWire.boolean(frame["ok"]) == true, let payload = frame["payload"] as? [String: Any],
              NativeTaskReceiptWire.integer(payload["contract_version"]) == 1, payload["session_id"] as? String == request.sessionID else { unconfirmed(); return true }
        if let flowID = request.flowID {
            guard Set(payload.keys) == ["contract_version", "session_id", "found", "receipt"], let found = NativeTaskReceiptWire.boolean(payload["found"]) else { unconfirmed(); return true }
            if !found {
                guard payload["receipt"] is NSNull else { unconfirmed(); return true }
                freshIDs.remove(flowID); status = "A previously recorded task is no longer available in this chat."; return true
            }
            guard let receipt = NativeTaskReceiptWire.receipt(payload["receipt"], sessionID: request.sessionID), receipt.id == flowID else { unconfirmed(); return true }
            merge([receipt])
        } else {
            guard Set(payload.keys) == ["contract_version", "session_id", "receipts", "next_cursor", "has_more"], let rows = payload["receipts"] as? [Any], rows.count <= 20,
                  let hasMore = NativeTaskReceiptWire.boolean(payload["has_more"]) else { unconfirmed(); return true }
            let next = NativeTaskReceiptWire.cursor(payload["next_cursor"])
            guard next != nil || payload["next_cursor"] is NSNull, !hasMore || next != nil else { unconfirmed(); return true }
            let parsed = rows.compactMap { NativeTaskReceiptWire.receipt($0, sessionID: request.sessionID) }
            guard parsed.count == rows.count, Set(parsed.map(\.id)).count == parsed.count else { unconfirmed(); return true }
            merge(parsed); if let next { cursor = next }; more = hasMore; pages += 1
            if hasMore && pages >= 5 { historyLimited = true }
        }
        status = receipts.isEmpty ? "No tracked background tasks are recorded for this chat." : "Recorded task snapshots; external action outcomes are not asserted."
        return true
    }
    private func unconfirmed() { freshIDs = []; status = "Task receipt response was unavailable or did not match this chat. Recorded snapshots are unconfirmed." }
    private func merge(_ incoming: [NativeTaskReceipt]) {
        var byID = Dictionary(uniqueKeysWithValues: receipts.map { ($0.id, $0) })
        for receipt in incoming {
            if let old = byID[receipt.id], receipt.updatedAt < old.updatedAt { freshIDs.remove(receipt.id); continue }
            byID[receipt.id] = receipt; freshIDs.insert(receipt.id)
        }
        receipts = byID.values.sorted { $0.createdAt == $1.createdAt ? $0.id < $1.id : $0.createdAt < $1.createdAt }
        if receipts.count > 100 { historyLimited = true; receipts = Array(receipts.suffix(100)); freshIDs.formIntersection(Set(receipts.map(\.id))) }
    }
}
