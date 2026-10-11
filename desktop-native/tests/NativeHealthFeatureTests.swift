import Foundation

private final class HealthWire: URLProtocol {
    static var replies: [String: [String: Any]] = [:]
    static var paths: [String] = []
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let path = request.url!.path
        Self.paths.append(path)
        let value = Self.replies[path] ?? ["error": "fixture missing"]
        let data = try! JSONSerialization.data(withJSONObject: value)
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: [:])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
private struct Failure: Error { let message: String }
private func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw Failure(message: message) }
}
@main struct HealthTests {
    @MainActor static func main() async throws {
        try expect(NativeHealthParsing.number(true) == nil && NativeHealthParsing.number("84") == nil && NativeHealthParsing.number(Double.nan) == nil, "invalid sensor values must remain unavailable")
        print("PASS invalid numeric types remain unavailable")
        let measures = NativeHealthParsing.measures(["current_hr": 145, "resting_hr": NSNull(), "current_hr_source": "Theora glasses", "synthetic_blood_pressure": 120])
        try expect(measures.first(where: {$0.id == "current_hr"})?.value == 145 && measures.first(where: {$0.id == "resting_hr"})?.value == nil, "current HR must never establish resting HR")
        try expect(measures.first(where: {$0.id == "current_hr"})?.source == "Theora glasses" && !measures.contains(where: {$0.id == "synthetic_blood_pressure"}), "only contracted measurements and source fields are consumed")
        print("PASS current/resting HR separation and contracted fields")
        let sourceFixture: [String: Any] = ["devices": [["node_id": "fixture-phone", "subdevices": [["capability": "jw_health_glasses", "live": false, "last_seen": 1700000000, "provenance": "phone_ble", "attrs": ["device_name": "Fixture glasses"]], ["capability": "generic_ble_hr"], ["capability": "microphone"]]]]]
        let sources = NativeHealthParsing.sources(sourceFixture)
        try expect(sources.count == 2 && sources[0].status == "Stale source" && sources[0].lastSeen?.timeIntervalSince1970 == 1700000000 && sources[0].provenance == "phone_ble", "stale source must retain actual heartbeat and provenance")
        try expect(sources[1].live == nil && sources[1].lastSeen == nil, "missing source status cannot be promoted to live")
        print("PASS source provenance, stale and unknown states")
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [HealthWire.self]
        let model = NativeHealthModel(baseURL: nil, session: URLSession(configuration: config))
        await model.refresh()
        try expect(HealthWire.paths.isEmpty && model.payloads.isEmpty && model.errors.count == 5, "unready agent must make no requests or invent measurements")
        print("PASS nil base URL makes no requests")
        model.setBaseURL(URL(string: "https://health.example.com")!)
        await model.refresh()
        try expect(HealthWire.paths.isEmpty && model.errors.count == 5, "remote base URL must be refused before requests")
        print("PASS remote health endpoints are refused")
        HealthWire.replies = ["/api/health-summary": ["data": ["current_hr": 145, "resting_hr": NSNull(), "current_hr_source": "Theora glasses"]], "/api/baseline/summary": ["metrics_tracked": 1, "recent_alerts": 0, "categories": ["cardiovascular"]], "/api/baseline/metrics": ["metrics": [["metric_id": "hr", "mean": 80, "std_dev": 4, "values": [76, 84], "last_updated": 1700000000]]], "/api/baseline/alerts": ["alerts": []], "/api/dashboard": sourceFixture]
        model.setBaseURL(URL(string: "http://127.0.0.1:9999")!)
        await model.refresh()
        try expect(model.errors.isEmpty && model.payloads.count == 5 && Set(HealthWire.paths) == Set(NativeHealthModel.paths.values), "all actual read-only API contracts must be requested")
        try expect(model.measures.first(where: {$0.id == "current_hr"})?.value == 145 && model.sources[0].name == "Theora glasses", "wire fixtures must feed the real model")
        print("PASS five independent API contracts")
        let lastFetch = model.fetchedAt["snapshot"]
        HealthWire.replies["/api/health-summary"] = ["error": "No health platforms connected", "data": [:]]
        HealthWire.replies["/api/dashboard"] = ["devices": [], "subdevices_unavailable": "fixture unavailable"]
        await model.refresh()
        try expect(model.errors["snapshot"] == "No health platforms connected" && model.fetchedAt["snapshot"] == lastFetch && model.measures.first(where: {$0.id == "current_hr"})?.value == 145, "200 error must preserve previous snapshot with an explicit refresh error")
        try expect(model.payloads["sources"]?["subdevices_unavailable"] as? String == "fixture unavailable", "source registry failures must remain visible")
        print("PASS error response and cached snapshot qualification")
        HealthWire.replies["/api/baseline/metrics"] = [:]
        await model.refresh()
        try expect(model.errors["metrics"] != nil, "malformed response cannot assert no baseline metrics")
        print("PASS malformed response does not become an empty result")
        model.setBaseURL(nil)
        try expect(model.payloads.isEmpty && model.fetchedAt.isEmpty, "new agent identity clears prior health data")
        print("PASS base URL changes clear old agent data")
    }
}
