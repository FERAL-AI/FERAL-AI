import Foundation

private struct TestFailure: Error { let message: String }
private func require(_ condition: @autoclosure () -> Bool, _ message: String) throws { if !condition() { throw TestFailure(message: message) } }

@main struct NativeTaskResultTests {
    static let sid = "task-result-native"
    static func row(id: String = "flow-a", result: Any = NSNull(), present: Bool = false, status: String = "waiting", processing: String = "waiting", digest: String = String(repeating: "a", count: 64), updated: Double = 100) -> [String: Any] {
        ["contract_version": 1, "flow_id": id, "origin_session_id": sid, "title": "Inert background task", "status": status, "current_step": 0, "steps_total": 1, "steps_completed": 0, "processing_outcome": processing, "action_outcome": "not_asserted", "result_present": present, "result_text": result, "result_truncated": false, "result_step_index": present ? 0 : NSNull(), "error_code": NSNull(), "result_digest": digest, "created_at": 100.0, "updated_at": updated]
    }
    static func page(_ rows: [[String: Any]], more: Bool = false, cursor: Any = NSNull()) -> [String: Any] { ["contract_version": 1, "session_id": sid, "receipts": rows, "has_more": more, "next_cursor": cursor] }
    static func response(_ id: String, _ payload: [String: Any], ok: Bool = true) -> [String: Any] { ["type": "res", "id": id, "ok": ok, "payload": payload] }
    @MainActor static func settle() async { for _ in 0..<10 { await Task.yield() } }
    @MainActor static func main() async {
        do {
            try require(NativeTaskReceiptWire.receipt(row(), sessionID: sid)?.resultPresent == false, "absent result")
            for text in ["", "false", "0", "[]", "{}", "null"] {
                let parsed = NativeTaskReceiptWire.receipt(row(result: text, present: true), sessionID: sid)
                try require(parsed?.resultPresent == true && parsed?.result == text, "falsy/empty result preserved")
            }
            print("PASS absence, empty text and falsy JSON result presence")
            for key in ["contract_version", "current_step", "steps_total", "steps_completed"] {
                var invalid = row(); invalid[key] = true
                try require(NativeTaskReceiptWire.receipt(invalid, sessionID: sid) == nil, "boolean is not integer")
            }
            for key in ["result_present", "result_truncated"] {
                var invalid = row(); invalid[key] = 0
                try require(NativeTaskReceiptWire.receipt(invalid, sessionID: sid) == nil, "integer is not boolean")
            }
            for value in [Double.nan, Double.infinity, -1.0] {
                var invalid = row(); invalid["updated_at"] = value
                try require(NativeTaskReceiptWire.receipt(invalid, sessionID: sid) == nil, "finite persisted time")
            }
            var bad = row(); bad["origin_session_id"] = "foreign"
            try require(NativeTaskReceiptWire.receipt(bad, sessionID: sid) == nil, "foreign origin")
            bad = row(); bad["action_outcome"] = "succeeded"
            try require(NativeTaskReceiptWire.receipt(bad, sessionID: sid) == nil, "external outcome cannot be asserted")
            bad = row(); bad["private_origin"] = "unexpected"
            try require(NativeTaskReceiptWire.receipt(bad, sessionID: sid) == nil, "exact public shape")
            bad = row(result: String(repeating: "é", count: 2049), present: true)
            try require(NativeTaskReceiptWire.receipt(bad, sessionID: sid) == nil, "UTF8 byte bound")
            bad = row(); bad["result_text"] = "false"
            try require(NativeTaskReceiptWire.receipt(bad, sessionID: sid) == nil, "presence cannot contradict text")
            print("PASS strict types, scope, privacy, result bounds and outcome boundary")

            var frames: [[String: Any]] = []
            let model = NativeTaskResultModel(automaticPolling: false), generation = UUID()
            model.configure(sessionID: sid, connectionID: generation) { frames.append($0) }
            model.tick(); await settle()
            try require(frames.isEmpty, "no capability no reads")
            model.negotiate(["session_id": sid], connectionID: generation); model.tick(); await settle()
            try require(frames.isEmpty && !model.supported, "old server is unavailable")
            model.negotiate(["session_id": sid, "task_result_contract_versions": [true]], connectionID: generation)
            try require(!model.supported, "malformed capability")
            model.negotiate(["session_id": sid, "task_result_contract_versions": [1]], connectionID: generation)
            model.tick(); await settle()
            let originalID = model.requestID!
            try require(frames.count == 1 && frames[0]["method"] as? String == "task.receipts", "bounded discovery read")
            model.tick(); await settle(); try require(frames.count == 1, "single flight")
            try require(!model.consume(response("foreign-id", page([row()])), connectionID: generation) && model.receipts.isEmpty, "response correlation")
            try require(model.consume(response(originalID, page([row()])), connectionID: UUID()) && model.receipts.isEmpty, "generation fence")
            model.consume(response(originalID, page([row()])), connectionID: generation)
            try require(model.receipts.count == 1 && model.freshIDs == ["flow-a"], "current scoped page")
            print("PASS capability negotiation, single-flight and response/generation correlation")

            model.tick(); await settle()
            let snapshotID = model.requestID!
            try require(frames.last?["method"] as? String == "task.receipt", "existing snapshot refresh")
            let complete = row(result: "false", present: true, status: "completed", processing: "completed", digest: String(repeating: "b", count: 64), updated: 101)
            model.consume(response(snapshotID, ["contract_version": 1, "session_id": sid, "found": true, "receipt": complete]), connectionID: generation)
            try require(model.receipts.count == 1 && model.receipts[0].result == "false" && model.receipts[0].label == "Processing completed", "updated processing snapshot replaces same job")
            model.tick(); await settle()
            model.consume(response(model.requestID!, ["contract_version": 1, "session_id": sid, "found": true, "receipt": row(status: "running", processing: "in_progress", updated: 102)]), connectionID: generation)
            try require(model.receipts[0].processing == "in_progress" && !model.receipts[0].resultPresent, "terminal snapshot remains mutable on resume")
            print("PASS digest dedup, terminal refresh/resume and truthful noncompletion")

            model.tick(); await settle(); let oldID = model.requestID!
            let nextGeneration = UUID()
            model.configure(sessionID: sid, connectionID: nextGeneration) { frames.append($0) }
            try require(model.receipts.count == 1 && model.freshIDs.isEmpty, "same chat reconnect retains unconfirmed")
            try require(!model.consume(response(oldID, page([complete])), connectionID: generation), "reconnect rejects old reply")
            model.negotiate(["session_id": sid, "task_result_contract_versions": [1]], connectionID: nextGeneration)
            model.tick(); await settle()
            var foreign = page([complete]); foreign["session_id"] = "foreign"
            model.consume(response(model.requestID!, foreign), connectionID: nextGeneration)
            try require(model.freshIDs.isEmpty && model.receipts[0].processing == "in_progress", "foreign reply cannot overwrite")
            model.tick(); await settle()
            model.consume(response(model.requestID!, page([complete]), ok: false), connectionID: nextGeneration)
            try require(model.freshIDs.isEmpty && model.status.contains("unconfirmed"), "read errors do not complete tasks")
            model.tick(); await settle(); let expiredID = model.requestID!
            model.consume(response(expiredID, page([complete])), connectionID: nextGeneration, now: Date().addingTimeInterval(9))
            try require(model.freshIDs.isEmpty && model.receipts[0].processing == "in_progress", "late reply rejected")
            model.configure(sessionID: "new-chat", connectionID: UUID())
            try require(model.receipts.isEmpty && model.requestID == nil && !model.supported, "selection clears old chat")
            print("PASS reconnect/selection/foreign/error/timeout fences")

            let bounded = NativeTaskResultModel(automaticPolling: false), boundedGeneration = UUID()
            bounded.configure(sessionID: sid, connectionID: boundedGeneration) { _ in }
            bounded.negotiate(["session_id": sid, "task_result_contract_versions": [1]], connectionID: boundedGeneration)
            for batch in 0..<5 {
                bounded.tick(); await settle()
                let rows = (0..<20).map { row(id: String(format: "flow-%03d", batch * 20 + $0)) }
                bounded.consume(response(bounded.requestID!, page(rows, more: true, cursor: ["created_at": 100.0, "flow_id": rows.last!["flow_id"]!])), connectionID: boundedGeneration)
            }
            try require(bounded.receipts.count == 100 && bounded.historyLimited, "five page bound")
            bounded.tick(); await settle()
            try require(bounded.requestID != nil, "bounded snapshot after page cap")
            bounded.consume(response(bounded.requestID!, ["contract_version": 1, "session_id": sid, "found": false, "receipt": NSNull()]), connectionID: boundedGeneration)
            bounded.tick(now: Date().addingTimeInterval(31)); await settle()
            bounded.consume(response(bounded.requestID!, page([row(id: "flow-new", updated: 103)])), connectionID: boundedGeneration)
            try require(bounded.receipts.count == 100 && bounded.receipts.contains(where: { $0.id == "flow-new" }), "bounded retention continues discovery")
            bounded.configure(sessionID: sid, connectionID: nil); await settle()
            try require(bounded.retainedTasks == 0, "owned tasks drained on disconnect")
            print("PASS bounded discovery/history/continued snapshots and lifecycle drain")

            if let fixture = ProcessInfo.processInfo.environment["FERAL_TASK_RESULT_GATEWAY_FIXTURE"] {
                let object = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: fixture))) as! [String: Any]
                // Shape is deliberately supplied by the actual gateway fixture;
                // no modeled action or external account is used by this harness.
                let receiptResponse = object["receipt_response"] as? [String: Any] ?? object["receipt"] as? [String: Any] ?? [:]
                let payload = receiptResponse["payload"] as? [String: Any] ?? [:]
                let actualSID = payload["session_id"] as? String ?? ""
                try require(!actualSID.isEmpty && NativeTaskReceiptWire.receipt(payload["receipt"], sessionID: actualSID) != nil, "actual gateway-generated receipt")
                print("PASS actual gateway-generated public receipt fixture")
            }
            model.configure(sessionID: nil, connectionID: nil)
            print("PASS NativeTaskResultFeature tests")
        } catch { print("FAIL \(error)"); exit(1) }
    }
}
