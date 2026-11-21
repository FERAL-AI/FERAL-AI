import Foundation
import CryptoKit

private struct CheckFailure: Error { let message: String }
private final class AttachmentProtocol: URLProtocol {
    static var status = 200
    static var response: [String: Any] = [:]
    static var requests: [URLRequest] = []
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.requests.append(request)
        let data = try! JSONSerialization.data(withJSONObject: Self.response)
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: Self.status, httpVersion: nil, headerFields: [:])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data); client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() { }
}

@main private enum AttachmentChecks {
    static var count = 0
    static func check(_ value: Bool, _ message: String) throws {
        guard value else { throw CheckFailure(message: message) }; count += 1
    }
    static func rejects(_ action: () async throws -> Void, _ message: String) async throws {
        var rejected = false
        do { try await action() } catch { rejected = true }
        try check(rejected, message)
    }
    static func main() async {
        do {
            let content = Data("disposable attachment fixture".utf8)
            let hash = SHA256.hash(data: content).map { String(format: "%02x", $0) }.joined()
            let record: [String: Any] = ["upload_id": "upload-fixture", "filename": "fixture.txt", "content_type": "text/plain", "size_bytes": content.count, "sha256": hash]
            let ref = try NativeAttachmentWire.receipt(record, expectedSize: content.count)
            try check(ref.id == "upload-fixture" && ref.sizeBytes == content.count, "valid complete receipt")
            try check(NSDictionary(dictionary: ref.record).isEqual(to: record), "canonical reference preserves all wire fields")
            for key in ["upload_id", "filename", "content_type", "sha256", "size_bytes"] {
                var invalid = record; invalid.removeValue(forKey: key)
                try await rejects({ _ = try NativeAttachmentWire.receipt(invalid, expectedSize: content.count) }, "missing \(key) rejected")
            }
            var invalid = record; invalid["size_bytes"] = true
            try await rejects({ _ = try NativeAttachmentWire.receipt(invalid, expectedSize: 1) }, "boolean size rejected")
            invalid = record; invalid["size_bytes"] = content.count - 1
            try await rejects({ _ = try NativeAttachmentWire.receipt(invalid, expectedSize: content.count) }, "partial size rejected")
            let body = NativeAttachmentWire.multipart(data: content, filename: "x\"\r\nInjected: bad.txt", contentType: "text/plain", boundary: "fixture-boundary")
            let bodyText = String(decoding: body, as: UTF8.self)
            try check(!bodyText.contains("\r\nInjected:"), "filename cannot inject a MIME header")
            try check(bodyText.contains("name=\"file\"") && bodyText.hasSuffix("--fixture-boundary--\r\n"), "canonical file field and final boundary")
            try check(body.range(of: content) != nil, "bytes preserved in multipart")
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent("feral-attachment-tests-" + UUID().uuidString)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            defer { try? FileManager.default.removeItem(at: directory) }
            let file = directory.appendingPathComponent("fixture.txt"); try content.write(to: file)
            let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [AttachmentProtocol.self]
            let session = URLSession(configuration: config); defer { session.invalidateAndCancel() }
            AttachmentProtocol.response = record
            let uploaded = try await NativeAttachmentWire.upload(file, baseURL: URL(string: "http://127.0.0.1:9462")!, session: session)
            try check(uploaded.sha256 == hash, "upload verifies full checksum")
            try check(AttachmentProtocol.requests.last?.url?.path == "/api/uploads" && AttachmentProtocol.requests.last?.httpMethod == "POST", "actual upload route and method")
            let previous = AttachmentProtocol.requests.count
            try await rejects({ _ = try await NativeAttachmentWire.upload(file, baseURL: URL(string: "https://example.com")!, session: session) }, "external origin rejected")
            try check(AttachmentProtocol.requests.count == previous, "origin rejection sends no bytes")
            for origin in ["http://localhost:9462", "http://user:secret@127.0.0.1:9462"] {
                try await rejects({ _ = try await NativeAttachmentWire.upload(file, baseURL: URL(string: origin)!, session: session) }, "DNS and credential URL rejected")
            }
            try check(AttachmentProtocol.requests.count == previous, "rejected connections send no file bytes")
            AttachmentProtocol.status = 413; AttachmentProtocol.response = ["detail": "fixture quota exceeded"]
            try await rejects({ _ = try await NativeAttachmentWire.upload(file, baseURL: URL(string: "http://127.0.0.1:9462")!, session: session) }, "quota failure does not create reference")
            AttachmentProtocol.status = 200; var mismatch = record; mismatch["sha256"] = String(repeating: "0", count: 64); AttachmentProtocol.response = mismatch
            try await rejects({ _ = try await NativeAttachmentWire.upload(file, baseURL: URL(string: "http://127.0.0.1:9462")!, session: session) }, "checksum mismatch rejected")
            let empty = directory.appendingPathComponent("empty.txt"); try Data().write(to: empty)
            let beforeEmpty = AttachmentProtocol.requests.count
            try await rejects({ _ = try await NativeAttachmentWire.upload(empty, baseURL: URL(string: "http://127.0.0.1:9462")!, session: session) }, "empty file rejected")
            try check(AttachmentProtocol.requests.count == beforeEmpty, "empty file sends no request")
            print("NATIVE_ATTACHMENT_TESTS_PASSED: \(count) assertions; mocked transport, no provider contact")
        } catch { fputs("NATIVE_ATTACHMENT_TESTS_FAILED: \(error)\n", stderr); exit(1) }
    }
}
