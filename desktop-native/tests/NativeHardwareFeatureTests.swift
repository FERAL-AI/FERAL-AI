import Foundation

@main struct NativeHardwareFeatureTests {
    static var count = 0
    static func check(_ condition: Bool, _ label: String) { guard condition else { fatalError("FAIL: " + label) }; count += 1 }
    static func refuses(_ label: String, _ operation: () throws -> Void) { do { try operation(); fatalError("Accepted: " + label) } catch { count += 1 } }
    static func cap(_ changes: [String: Any] = [:]) -> [String: Any] {
        var value: [String: Any] = ["id": "read_sensor", "name": "Read sensor", "description": "Read private fixture sensor data", "category": "sensor", "permission_tier": "passive", "requires_confirmation": false, "action_type": "read", "parameters": [["name": "metric", "type": "string", "required": true, "enum": ["heart_rate", "battery"]], ["name": "samples", "type": "integer", "minimum": 1, "maximum": 5], ["name": "raw", "type": "boolean"]], "verify": NSNull()]
        for (key, child) in changes { value[key] = child }; return value
    }
    static func manifest(_ capability: [String: Any] = cap()) -> [String: Any] { ["device_id": "fixture-node", "name": "Fixture glasses", "connection_type": "websocket", "device_type": "glasses", "capabilities": [capability], "firmware_version": "1"] }
    static func mesh(_ registered: Double = 100) -> [String: Any] { ["nodes": [["node_id": "fixture-node", "online": true, "registered_at": registered, "generation": "22222222-2222-4222-8222-222222222222", "node_type": "glasses", "platform": "fixture"]], "announced_devices": []] }
    @MainActor static func main() async throws {
        let declared = manifest(), params: [String: Any] = ["metric": "heart_rate", "samples": 2, "raw": false]
        _ = try NativeHardwareWire.manifest(declared, deviceID: "fixture-node")
        check(try NativeHardwareWire.capability(declared, command: "read_sensor", params: params)["id"] as? String == "read_sensor", "declared passive scalar-schema method accepted")
        for changes in ([["category": "actuator"], ["permission_tier": "dangerous"], ["permission_tier": "privileged"], ["requires_confirmation": true], ["requires_confirmation": 0], ["action_type": "execute"]] as [[String: Any]]) {
            refuses("unsafe or separately confirmed declaration remains inspection") { _ = try NativeHardwareWire.capability(manifest(cap(changes)), command: "read_sensor", params: params) }
        }
        refuses("wrong device detail") { _ = try NativeHardwareWire.manifest(declared, deviceID: "foreign") }
        refuses("duplicate methods") { var duplicate = declared; duplicate["capabilities"] = [cap(), cap()]; _ = try NativeHardwareWire.manifest(duplicate, deviceID: "fixture-node") }
        refuses("required missing") { _ = try NativeHardwareWire.capability(declared, command: "read_sensor", params: [:]) }
        refuses("undeclared parameter") { _ = try NativeHardwareWire.capability(declared, command: "read_sensor", params: ["metric": "battery", "shell": "do not execute"]) }
        refuses("enum value") { _ = try NativeHardwareWire.capability(declared, command: "read_sensor", params: ["metric": "unknown"]) }
        refuses("boolean not integer") { _ = try NativeHardwareWire.capability(declared, command: "read_sensor", params: ["metric": "battery", "samples": true]) }
        refuses("numeric not bool") { _ = try NativeHardwareWire.capability(declared, command: "read_sensor", params: ["metric": "battery", "raw": 1]) }
        refuses("range bound") { _ = try NativeHardwareWire.capability(declared, command: "read_sensor", params: ["metric": "battery", "samples": 6]) }
        refuses("unsupported nested schema") { _ = try NativeHardwareWire.capability(manifest(cap(["parameters": [["name": "query", "type": "object"]]])), command: "read_sensor", params: [:]) }
        check(try NativeHardwareWire.liveNode(["nodes": []], deviceID: "fixture-node") == nil, "empty mesh means no matching live node, not fabricated hardware")
        refuses("unknown mesh") { _ = try NativeHardwareWire.liveNode([:], deviceID: "fixture-node") }
        var numericOnline = mesh(); numericOnline["nodes"] = [["node_id": "fixture-node", "online": 1, "registered_at": 100]]
        refuses("strict live boolean") { _ = try NativeHardwareWire.liveNode(numericOnline, deviceID: "fixture-node") }
        let origin = URL(string: "http://127.0.0.1:9465")!
        var declaredNow = declared, meshNow = mesh(), requests: [URLRequest] = [], result: [String: Any] = ["state": "submitted", "queued": true, "acknowledged": false, "device_reported_success": NSNull()]
        let serverReviewID = "11111111-1111-4111-8111-111111111111", serverCommandID = "33333333-3333-4333-8333-333333333333"
        var reviewedParams: [String: Any] = [:], holdServerReview = false, mismatchedServerReview = false
        var failPost = false, unknownCounters = false, holdPath: String?, continuation: CheckedContinuation<Void, Never>?
        let model = NativeHardwareModel(transport: { request in
            requests.append(request)
            let path = request.url!.path
            if holdPath == path { holdPath = nil; await withCheckedContinuation { continuation = $0 } }
            if failPost && path.hasSuffix("/dispatch") { throw NativeHardwareFailure("Transport receipt unavailable.") }
            let body: [String: Any]
            switch path {
            case "/api/hardware/devices": body = ["devices": [declaredNow]]
            case "/api/hardware/context": body = ["context": "Fixture context only"]
            case "/api/hardware/stats": body = unknownCounters ? [:] : ["device_count": 1, "total_capabilities": 1, "total_sensors": 1, "total_actuators": 0, "actions_executed": 0]
            case "/api/hardware/device/fixture-node": body = declaredNow
            case "/api/hardware/mesh": body = meshNow
            case "/api/sessions/primary": body = ["session_id": "fixture-primary"]
            case "/api/hardware/reviewed/review":
                let posted = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
                reviewedParams = posted["params"] as! [String: Any]
                let now = Date().timeIntervalSince1970
                body = ["contract_version": 1, "separate_authorization_required": holdServerReview,
                    "review": ["review_id": serverReviewID, "owner": "operator:fixture-primary", "node_id": "fixture-node",
                        "generation": "22222222-2222-4222-8222-222222222222", "command": mismatchedServerReview ? "shell" : "read_sensor",
                        "params_json": String(decoding: try NativeHardwareWire.freeze(reviewedParams), as: UTF8.self),
                        "manifest_json": String(decoding: try NativeHardwareWire.freeze(declaredNow), as: UTF8.self),
                        "permission_tier": "passive", "requires_confirmation": holdServerReview,
                        "timeout": posted["timeout"]!, "created_at": now, "expires_at": now + 300]]
            default:
                var ledger: [String: Any] = ["contract_version": 1, "owner": "operator:fixture-primary", "review_id": serverReviewID,
                    "command_id": serverCommandID, "generation": "22222222-2222-4222-8222-222222222222",
                    "node_id": "fixture-node", "command": "read_sensor", "params": reviewedParams,
                    "permission_tier": "passive", "requires_confirmation": false,
                    "physical_outcome_verified": false, "automatic_retry": false, "effect": "unknown", "device_report": NSNull()]
                for (key, value) in result { ledger[key] = value }; body = ledger
            }
            return (try JSONSerialization.data(withJSONObject: body), HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        })
        model.configure(baseURL: origin); await model.refresh(); await model.inspect("fixture-node")
        check(model.detail != nil && model.node != nil && requests.allSatisfy { $0.httpMethod == "GET" }, "inspection reads real inventory/detail/mesh without invocation")
        let review = try model.review(command: "read_sensor", params: params, timeout: 10)
        check(review.explanation.contains("heart_rate") && review.explanation.contains("private") && review.explanation.contains("not physical certification"), "review exposes exact params and privacy/evidence limits")
        check(await model.perform(review), "reviewed passive read obtains exact server queue receipt")
        let dispatchRequest = requests.last!, dispatchBody = try JSONSerialization.jsonObject(with: dispatchRequest.httpBody!) as! [String: Any]
        let serverReviewRequest = requests.first { $0.url?.path == "/api/hardware/reviewed/review" }!
        let posted = try JSONSerialization.jsonObject(with: serverReviewRequest.httpBody!) as! [String: Any]
        check(dispatchRequest.url?.path == "/api/hardware/reviewed/" + serverReviewID + "/dispatch" && dispatchBody.isEmpty && posted["node_id"] as? String == "fixture-node" && posted["command"] as? String == "read_sensor" && posted["timeout"] as? Double == 10 && posted["params"] as? [String: Any] != nil, "server exact review then opaque one-use dispatch without body authority")
        check(requests.allSatisfy { $0.url?.path != "/api/hardware/invoke" }, "native never uses legacy unguarded invoke")
        check(model.receipt?.state == "queued_unverified" && model.receipt?.physicalOutcomeVerified == false && model.receipt?.echoedScope == true && model.receipt?.commandID == serverCommandID, "server UUID queue receipt never proves physical outcome")
        let postCount = requests.filter { $0.httpMethod == "POST" }.count
        check(!(await model.perform(review)) && requests.filter { $0.httpMethod == "POST" }.count == postCount, "one-use reviewed dispatch does not replay")
        let now = Date().timeIntervalSince1970
        let serverBody: [String: Any] = ["contract_version": 1, "separate_authorization_required": false, "review": [
            "review_id": serverReviewID, "owner": "operator:fixture-primary", "node_id": "fixture-node", "command": "read_sensor",
            "generation": "22222222-2222-4222-8222-222222222222", "permission_tier": "passive", "requires_confirmation": false,
            "params_json": String(decoding: review.params, as: UTF8.self), "manifest_json": String(decoding: review.manifest, as: UTF8.self),
            "timeout": 10, "created_at": now, "expires_at": now + 300]]
        let serverHeld = try NativeHardwareWire.serverReview(serverBody, local: review, primaryID: "fixture-primary")
        let canonical = model.receipt!.raw
        for (changes, expected) in [(["state": "submitted", "queued": true, "acknowledged": false, "device_reported_success": NSNull()] as [String: Any], "queued_unverified"),
            (["state": "acked", "queued": true, "acknowledged": true, "device_reported_success": NSNull()], "acknowledged_unverified"),
            (["state": "succeeded", "queued": true, "acknowledged": true, "device_reported_success": true, "device_report": ["success": true, "data": ["reading": 36]]], "device_reported_success_unverified"),
            (["state": "timed_out", "queued": true, "acknowledged": false, "device_reported_success": NSNull()], "device_reported_failure_effect_unknown")] {
            var body = canonical; for (key, value) in changes { body[key] = value }
            check(try NativeHardwareWire.receipt(body, review: serverHeld).state == expected, "queue ACK report and timeout remain separate")
        }
        for changes in ([["owner": "operator:foreign"], ["node_id": "foreign"], ["command": "shell"], ["command_id": "truncated"], ["physical_outcome_verified": true], ["acknowledged": 1], ["device_reported_success": 1], ["state": "surprise"]] as [[String: Any]]) {
            refuses("foreign or malformed strict ledger receipt") { var body = canonical; for (key, value) in changes { body[key] = value }; _ = try NativeHardwareWire.receipt(body, review: serverHeld) }
        }
        for privateReport in (["serialized fallback", ["success": 1], ["success": false, "data": String(repeating: "x", count: 65_537)], ["success": true, "node_id": "foreign"]] as [Any]) {
            refuses("private report malformed oversized contradictory or foreign") { var body = canonical; body["state"] = "succeeded"; body["device_reported_success"] = true; body["device_report"] = privateReport; _ = try NativeHardwareWire.receipt(body, review: serverHeld) }
        }
        refuses("missing private report availability") { var body = canonical; body.removeValue(forKey: "device_report"); _ = try NativeHardwareWire.receipt(body, review: serverHeld) }
        refuses("foreign readback UUID") { _ = try NativeHardwareWire.receipt(canonical, review: serverHeld, expectedCommandID: UUID().uuidString.lowercased()) }
        for changes in ([["owner": "operator:foreign"], ["generation": UUID().uuidString.lowercased()], ["command": "shell"], ["requires_confirmation": true], ["permission_tier": "dangerous"], ["expires_at": now - 1]] as [[String: Any]]) {
            refuses("foreign stale or elevated server review") { var body = serverBody, item = body["review"] as! [String: Any]; for (key, value) in changes { item[key] = value }; body["review"] = item; _ = try NativeHardwareWire.serverReview(body, local: review, primaryID: "fixture-primary") }
        }
        let writesBeforeReadback = requests.filter { $0.httpMethod == "POST" }.count
        result = ["state": "acked", "queued": true, "acknowledged": true, "device_reported_success": NSNull()]
        await model.refreshReceipt()
        check(model.receipt?.state == "acknowledged_unverified" && requests.last?.httpMethod == "GET" && requests.filter { $0.httpMethod == "POST" }.count == writesBeforeReadback, "manual exact ledger readback never redispatches")
        result["command_id"] = UUID().uuidString.lowercased(); await model.refreshReceipt()
        check(model.receipt?.state == "acknowledged_unverified" && model.error != nil, "foreign readback leaves prior evidence and explicit unavailable status")
        result = ["state": "submitted", "queued": true, "acknowledged": false, "device_reported_success": NSNull()]
        await model.inspect("fixture-node")
        let drift = try model.review(command: "read_sensor", params: params, timeout: 10)
        declaredNow["firmware_version"] = "2"
        check(!(await model.perform(drift)) && requests.filter { $0.httpMethod == "POST" }.count == postCount, "manifest drift refuses write")
        await model.inspect("fixture-node")
        let replacement = try model.review(command: "read_sensor", params: params, timeout: 10)
        meshNow = mesh(200)
        check(!(await model.perform(replacement)) && requests.filter { $0.httpMethod == "POST" }.count == postCount, "same-ID reconnect registration generation refuses write")
        await model.inspect("fixture-node")
        holdServerReview = true
        let separatelyConfirmed = try model.review(command: "read_sensor", params: params, timeout: 10)
        let dispatchesBefore = requests.filter { $0.url?.path.hasSuffix("/dispatch") == true }.count
        check(!(await model.perform(separatelyConfirmed)) && requests.filter { $0.url?.path.hasSuffix("/dispatch") == true }.count == dispatchesBefore, "server policy additional confirmation cannot be auto-approved by passive native controls")
        holdServerReview = false; mismatchedServerReview = true; await model.inspect("fixture-node")
        let wrongServer = try model.review(command: "read_sensor", params: params, timeout: 10)
        check(!(await model.perform(wrongServer)) && requests.filter { $0.url?.path.hasSuffix("/dispatch") == true }.count == dispatchesBefore, "server altered review prevents actual dispatch")
        mismatchedServerReview = false; await model.inspect("fixture-node")
        let failed = try model.review(command: "read_sensor", params: params, timeout: 10); failPost = true
        check(!(await model.perform(failed)) && model.receipt == nil && model.error?.contains("may have reached") == true, "missing post-write receipt is explicitly uncertain")
        let failedCount = requests.count
        check(!(await model.perform(failed)) && requests.count == failedCount, "uncertain invocation never blind retries")
        failPost = false; await model.inspect("fixture-node")
        let stale = try model.review(command: "read_sensor", params: params, timeout: 10); holdPath = "/api/hardware/device/fixture-node"
        let pending = Task { await model.perform(stale) }
        while continuation == nil { await Task.yield() }
        let writesBefore = requests.filter { $0.httpMethod == "POST" }.count
        model.configure(baseURL: URL(string: "http://127.0.0.1:9466")!); continuation?.resume(); continuation = nil
        check(!(await pending.value) && requests.filter { $0.httpMethod == "POST" }.count == writesBefore && model.receipt == nil, "connection change during preflight cannot send on replacement backend")
        unknownCounters = true; await model.refresh()
        check(model.devices?.count == 1 && model.stats == nil && model.error != nil, "unknown counters do not erase valid device inventory or fabricate zero")
        model.configure(baseURL: URL(string: "http://example.com")!); let beforeRemote = requests.count; await model.refresh()
        check(requests.count == beforeRemote && model.devices == nil, "remote origin refused before transport")
        print("NativeHardwareFeatureTests: \(count) assertions passed; mocked wire only, no hardware execution")
    }
}
