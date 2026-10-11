import Foundation
private final class MemoryContextWire: URLProtocol {
    static var calls: [URLRequest] = []
    static var response: [String: Any] = ["count": 1, "snapshots": [["session_id": "fixture-session", "query": "fixture query", "memory_filter": "coding", "memory_context": "## Recent Context\nWorking fixture\n## Known Facts\nFact fixture\n## Recent Actions\nAction fixture", "latency_ms": 12, "ts": 1_790_000_000]]]
    static var fail = false
    static var hook: (() -> Void)?
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.calls.append(request)
        if Self.fail { client?.urlProtocol(self, didFailWithError: NSError(domain: "fixture", code: 3, userInfo: [NSLocalizedDescriptionKey: "Bearer private-context-sentinel"])); return }
        Self.hook?(); Self.hook = nil
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: [:])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: Self.response)); client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
@main private struct NativeMemoryContextFeatureTests {
    @MainActor static func main() async {
        var count = 0
        func check(_ condition: Bool, _ message: String) { precondition(condition, message); count += 1 }
        let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [MemoryContextWire.self]
        let session = URLSession(configuration: configuration)
        let local = URL(string: "http://127.0.0.1:19998")!
        let model = NativeMemoryContextModel(baseURL: local, session: session)
        check(model.snapshots == nil && MemoryContextWire.calls.isEmpty, "construction cannot invoke searches or providers")
        await model.refresh()
        check(MemoryContextWire.calls.count == 1 && MemoryContextWire.calls[0].httpMethod == "GET" && MemoryContextWire.calls[0].url?.path == "/api/memory/context", "only recorded context GET")
        check(URLComponents(url: MemoryContextWire.calls[0].url!, resolvingAgainstBaseURL: false)?.queryItems == [URLQueryItem(name: "limit", value: "20")], "bounded latest twenty request")
        check(model.snapshots?.count == 1 && model.loadedAt != nil && model.error == nil && !model.stale, "valid complete recorded response")
        let snapshot = model.snapshots![0]
        check(snapshot.session == "fixture-session" && snapshot.query == "fixture query" && snapshot.specialist == "coding" && snapshot.latency == 12, "specialist/session/query/timing preserved")
        check(snapshot.layers.map(\.title) == ["Recent Context", "Known Facts", "Recent Actions"], "actual source heading layers")
        check(snapshot.layers[1].body == "Fact fixture", "source body unchanged")
        let initial = MemoryContextWire.response
        let loadedAt = model.loadedAt
        MemoryContextWire.fail = true; await model.refresh()
        check(model.stale && model.snapshots?.count == 1 && model.loadedAt == loadedAt, "failed refresh retains clearly stale successful data")
        check(model.error?.contains("private-context-sentinel") == false, "exception details withheld")
        MemoryContextWire.fail = false
        MemoryContextWire.response = ["error": "Bearer private-context-sentinel"]
        await model.refresh(); check(model.stale && model.error?.contains("private-context-sentinel") == false, "private backend error cannot reach inspector")
        MemoryContextWire.response = ["count": 0, "snapshots": []]
        await model.refresh(); check(model.snapshots?.isEmpty == true && model.error == nil && !model.stale, "honest recorded-empty state")
        MemoryContextWire.response = ["count": 2, "snapshots": initial["snapshots"]!]
        await model.refresh(); check(model.error != nil && model.stale, "count mismatch cannot masquerade as complete state")
        MemoryContextWire.response = ["count": true, "snapshots": initial["snapshots"]!]
        await model.refresh(); check(model.error != nil, "boolean count refused")
        var row = (initial["snapshots"] as! [[String: Any]])[0]
        row["memory_context"] = ""
        MemoryContextWire.response = ["count": 1, "snapshots": [row]]
        await model.refresh(); check(model.snapshots?.first?.layers.isEmpty == true && model.error == nil, "empty block is valid and not an invented source failure")
        row["memory_context"] = "## Duplicate\nfirst\n## Duplicate\nsecond"
        MemoryContextWire.response = ["count": 1, "snapshots": [row]]
        await model.refresh(); check(model.snapshots?.first?.layers.map(\.id) == [0, 1] && model.snapshots?.first?.layers[1].body == "second", "duplicate headings have distinct stable local IDs")
        row["latency_ms"] = true
        MemoryContextWire.response = ["count": 1, "snapshots": [row]]
        await model.refresh(); check(model.error != nil, "boolean latency cannot be displayed as numeric timing")
        row["latency_ms"] = -1
        MemoryContextWire.response = ["count": 1, "snapshots": [row]]
        await model.refresh(); check(model.error != nil, "negative timing refused")
        row["latency_ms"] = 0; row["memory_context"] = String(repeating: "x", count: 128 * 1024 + 1)
        MemoryContextWire.response = ["count": 1, "snapshots": [row]]
        await model.refresh(); check(model.error != nil, "oversized source block refused without silent truncation")
        MemoryContextWire.response = ["count": 21, "snapshots": Array(repeating: (initial["snapshots"] as! [[String: Any]])[0], count: 21)]
        await model.refresh(); check(model.error != nil, "response cannot exceed requested row limit")
        MemoryContextWire.response = initial
        MemoryContextWire.hook = { model.configure(nil) }
        await model.refresh(); check(model.snapshots == nil && model.loadedAt == nil && !model.loading, "agent change clears private data and rejects stale response")
        model.configure(URL(string: "https://example.com")!)
        let before = MemoryContextWire.calls.count; await model.refresh()
        check(MemoryContextWire.calls.count == before && model.error != nil, "no context fetch outside literal loopback")
        model.configure(URL(string: "http://user:password@127.0.0.1")!); await model.refresh()
        check(MemoryContextWire.calls.count == before, "credential-bearing origin refused")
        print("Native memory context feature: \(count) assertions passed")
    }
}
