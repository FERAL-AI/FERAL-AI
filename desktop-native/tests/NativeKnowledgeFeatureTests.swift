import Foundation
private final class KnowledgeWire: URLProtocol {
    static var calls: [(String, String, [String: Any])] = []
    static var wrongID = false, malformed = false, failure = false, duplicate = false, badReceipt = false
    static var hook: (() -> Void)?
    static var multipart = Data()
    static var multipartType = ""
    static var wrongPDFHash = false
    static var wrongFolderHash = false
    static var row: [String: Any] = ["id": "note.fixture", "title": "Fixture", "kind": "note", "updated_at": 1_790_823_600.125, "source_refs": [["type": "note", "id": "fixture", "opaque_metadata": ["preserve": true]]]]
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var data = request.httpBody ?? Data()
        if let stream = request.httpBodyStream { stream.open(); defer { stream.close() }; var bytes = [UInt8](repeating: 0, count: 4096); while stream.hasBytesAvailable { let n = stream.read(&bytes, maxLength: bytes.count); if n <= 0 { break }; data.append(bytes, count: n) } }
        let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        let path = request.url!.path, method = request.httpMethod ?? "GET"; Self.calls.append((path, method, body))
        var value: [String: Any]
        switch path {
        case "/api/wiki/pages": value = ["pages": Self.malformed ? [["id": "bad"]] : (Self.duplicate ? [Self.row, Self.row] : [Self.row])]
        case "/api/wiki/stats": value = ["pages": 1, "kinds": [["kind": "note", "count": 1]]]
        case "/api/wiki/pages/note.fixture": value = Self.row; value["body_markdown"] = "# Native fixture\nOriginal document"; if Self.wrongID { value["id"] = "wrong" }
        case "/api/wiki/ingest/text": value = ["ok": !Self.badReceipt, "source": "text", "source_label": body["source_label"] ?? "", "notes_saved": 2, "compile": ["compiled": false]]
        case "/api/wiki/compile": value = ["compiled": !Self.badReceipt, "total_pages": 4]
        case "/api/wiki/ingest/pdf":
            Self.multipart = data; Self.multipartType = request.value(forHTTPHeaderField: "Content-Type") ?? ""
            let text = String(decoding: data, as: UTF8.self)
            let marker = "name=\"expected_sha256\"\r\n\r\n"
            let hash = text.components(separatedBy: marker).last?.components(separatedBy: "\r\n").first ?? ""
            let compile = text.contains("name=\"compile_after\"\r\n\r\ntrue\r\n")
            value = ["ok": true, "source": "pdf", "snapshot_sha256": Self.wrongPDFHash ? "wrong" : hash, "notes_saved": 3, "pages_read": 1, "compile": ["compiled": compile]]
        case "/api/wiki/ingest/repo":
            var files = body["expected_files"] as? [String: String] ?? [:]
            if Self.wrongFolderHash, let key = files.keys.first { files[key] = String(repeating: "0", count: 64) }
            value = ["ok": true, "source": "repo", "path": body["path"] ?? "", "snapshot_files": files, "files_processed": files.count, "notes_saved": files.count, "compile": ["compiled": body["compile_after"] ?? false]]
        default: value = ["error": "unexpected fixture route"]
        }
        if Self.failure { value = ["error": "Bearer fixture-private-secret"] }
        Self.hook?(); Self.hook = nil
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: [:])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: value)); client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
@main private struct NativeKnowledgeFeatureTests {
    @MainActor static func main() async throws {
        var count = 0
        func check(_ value: Bool, _ message: String) { precondition(value, message); count += 1 }
        func refuses(_ label: String, _ make: () throws -> Void) { do { try make(); preconditionFailure("Expected refusal: " + label) } catch { count += 1 } }
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [KnowledgeWire.self]; let session = URLSession(configuration: config)
        let local = URL(string: "http://127.0.0.1:19998")!
        let model = NativeKnowledgeModel(baseURL: local, session: session)
        await model.refresh(query: "don't & follow", kind: "note")
        check(KnowledgeWire.calls.count == 2 && KnowledgeWire.calls.allSatisfy { $0.1 == "GET" }, "navigation only passive wiki reads")
        check(model.pages.count == 1 && model.total == 1 && model.kinds == ["note"], "real listing and stats schema")
        check(model.pages[0].references[0].contains("opaque_metadata"), "source reference metadata survives rendering")
        await model.open("note.fixture")
        check(model.page?.id == "note.fixture" && model.page?.body.contains("Original document") == true, "generated dotted IDs open full stored page")
        check(model.page?.updated.isEmpty == false && model.pages.first?.updated == model.page?.updated, "real backend fractional epoch timestamp works in listing and detail")
        let originalRow = KnowledgeWire.row
        KnowledgeWire.row["updated_at"] = "2026-10-01T03:00:00Z"
        await model.refresh(); await model.open("note.fixture")
        check(model.error == nil && model.page?.updated.isEmpty == false, "strict ISO timestamp interoperability")
        let badTimestamps: [Any] = [true, -1, "arbitrary timestamp private sentinel", ["unknown": "object"]]
        for badTimestamp in badTimestamps {
            KnowledgeWire.row["updated_at"] = badTimestamp; await model.refresh()
            check(model.pages.isEmpty && model.error != nil && model.error?.contains("private sentinel") == false, "invalid timestamp shape refused without private-value echo")
        }
        KnowledgeWire.row = originalRow; await model.refresh()
        KnowledgeWire.wrongID = true; await model.open("note.fixture"); check(model.page == nil && model.error != nil, "detail identifier cannot be substituted"); KnowledgeWire.wrongID = false
        let calls = KnowledgeWire.calls.count; await model.open("../private"); check(KnowledgeWire.calls.count == calls, "unlisted page cannot trigger arbitrary path read")
        let text = "Exact native import \n keep whitespace "
        let review = try model.reviewText(content: text, sourceLabel: "fixture")
        check(review.content == text && review.scope.contains("external providers") && review.scope.contains("duplicate"), "exact text and asynchronous external embedding risk disclosed")
        check(!KnowledgeWire.calls.contains { $0.1 == "POST" }, "review does not import")
        check(await model.perform(review), "reviewed text import")
        let write = KnowledgeWire.calls.last!
        check(write.0 == "/api/wiki/ingest/text" && write.2["content"] as? String == text && write.2["compile_after"] as? Bool == false && write.2.count == 3, "exact text without implicit compilation or overwritten siblings")
        check(model.receipt?.contains("2 notes") == true, "actual note count receipt")
        let after = KnowledgeWire.calls.count; check(await model.perform(review) == false && KnowledgeWire.calls.count == after, "single use prevents duplicate retry")
        let canceled = try model.reviewText(content: "canceled", sourceLabel: "fixture"); model.cancel(canceled); check(await model.perform(canceled) == false, "cancel revokes capability")
        refuses("blank text") { _ = try model.reviewText(content: "   ", sourceLabel: "fixture") }
        refuses("oversized text") { _ = try model.reviewText(content: String(repeating: "a", count: 65_537), sourceLabel: "fixture") }
        refuses("NUL text") { _ = try model.reviewText(content: "a\0b", sourceLabel: "fixture") }
        refuses("oversized source label") { _ = try model.reviewText(content: "a", sourceLabel: String(repeating: "x", count: 257)) }
        let compile = try model.reviewCompile(); check(compile.scope.contains("Old derived pages can remain"), "compile no erasure guarantee")
        check(await model.perform(compile), "compile verified receipt")
        check(KnowledgeWire.calls.last!.2["notes_limit"] as? Int == 200 && KnowledgeWire.calls.last!.2["knowledge_limit"] as? Int == 400 && KnowledgeWire.calls.last!.2.count == 3, "compile bounded actual backend limits")
        let stale = try model.reviewCompile(); model.configure(nil); let beforeStale = KnowledgeWire.calls.count; check(await model.perform(stale) == false && KnowledgeWire.calls.count == beforeStale, "agent switch revokes held review")
        check(model.pages.isEmpty && model.page == nil && model.total == nil, "agent change clears private displayed data")
        model.configure(local); await model.refresh()
        KnowledgeWire.duplicate = true; await model.refresh(); check(model.pages.isEmpty && model.error != nil, "duplicate page IDs rejected"); KnowledgeWire.duplicate = false
        KnowledgeWire.malformed = true; await model.refresh(); check(model.pages.isEmpty && model.total == nil, "malformed pages cannot masquerade as empty successful list"); KnowledgeWire.malformed = false
        KnowledgeWire.failure = true; await model.refresh(); check(model.error?.contains("fixture-private-secret") == false, "backend exception secret withheld"); KnowledgeWire.failure = false
        KnowledgeWire.badReceipt = true; let bad = try model.reviewText(content: "receipt fixture", sourceLabel: "fixture"); check(await model.perform(bad) == false && model.receipt == nil && model.error?.contains("may already") == true, "incomplete write receipt exposes uncertainty without success"); KnowledgeWire.badReceipt = false
        let held = try model.reviewCompile(); KnowledgeWire.hook = { model.configure(nil) }; check(await model.perform(held) == false && model.receipt == nil, "in-flight switch cannot publish stale write receipt")
        model.configure(URL(string: "https://example.com")!); let external = try model.reviewCompile(); let externalBefore = KnowledgeWire.calls.count; check(await model.perform(external) == false && KnowledgeWire.calls.count == externalBefore, "non-loopback knowledge writes refused")
        model.configure(local)
        let pdfBytes = Data("%PDF-1.4 disposable bytes\n%%EOF".utf8)
        let pdfCalls = KnowledgeWire.calls.count
        let pdf = try model.reviewPDF(bytes: pdfBytes, filename: "fixture.pdf")
        check(KnowledgeWire.calls.count == pdfCalls && pdf.pdfBytes == pdfBytes && pdf.pdfSHA256?.count == 64, "PDF review freezes exact bytes/hash without upload")
        check(pdf.scope.contains("external providers") && pdf.scope.contains("Partial writes") && pdf.scope.contains("100 pages"), "PDF review discloses embedding and write bounds")
        check(await model.perform(pdf), "reviewed PDF multipart receipt")
        check(KnowledgeWire.calls.last?.0 == "/api/wiki/ingest/pdf" && KnowledgeWire.calls.last?.1 == "POST" && KnowledgeWire.multipartType.hasPrefix("multipart/form-data; boundary=FERALWiki-"), "genuine multipart PDF endpoint")
        check(KnowledgeWire.multipart.range(of: pdfBytes) != nil && String(decoding: KnowledgeWire.multipart, as: UTF8.self).contains(pdf.pdfSHA256!), "exact frozen bytes and reviewed digest sent")
        check(String(decoding: KnowledgeWire.multipart, as: UTF8.self).contains("name=\"compile_after\"\r\n\r\nfalse\r\n"), "compilation opt-in false stays false")
        let pdfAfter = KnowledgeWire.calls.count; check(await model.perform(pdf) == false && KnowledgeWire.calls.count == pdfAfter, "PDF replay cannot duplicate notes")
        let withCompile = try model.reviewPDF(bytes: pdfBytes, filename: "fixture.pdf", compileAfter: true); check(await model.perform(withCompile), "requested PDF compilation receipt validated")
        let canceledPDF = try model.reviewPDF(bytes: pdfBytes, filename: "fixture.pdf"); model.cancel(canceledPDF); check(await model.perform(canceledPDF) == false, "canceled PDF releases held upload capability")
        refuses("oversized PDF") { _ = try model.reviewPDF(bytes: Data(repeating: 1, count: 10 * 1024 * 1024 + 1), filename: "fixture.pdf") }
        refuses("missing PDF header") { _ = try model.reviewPDF(bytes: Data("not PDF".utf8), filename: "fixture.pdf") }
        refuses("multipart filename newline") { _ = try model.reviewPDF(bytes: pdfBytes, filename: "bad\r\nfilename.pdf") }
        refuses("path-shaped PDF filename") { _ = try model.reviewPDF(bytes: pdfBytes, filename: "../private.pdf") }
        refuses("combining-mark quoted PDF filename") { _ = try model.reviewPDF(bytes: pdfBytes, filename: "bad\"\u{0301}.pdf") }
        refuses("combining-mark backslash PDF filename") { _ = try model.reviewPDF(bytes: pdfBytes, filename: "bad\\\u{0301}.pdf") }
        refuses("combining-mark slash PDF filename") { _ = try model.reviewPDF(bytes: pdfBytes, filename: "bad/\u{0301}.pdf") }
        let wrongPDF = try model.reviewPDF(bytes: pdfBytes, filename: "fixture.pdf"); KnowledgeWire.wrongPDFHash = true
        check(await model.perform(wrongPDF) == false && model.receipt == nil && model.error?.contains("may already") == true, "different backend snapshot digest is uncertain failure")
        KnowledgeWire.wrongPDFHash = false
        let stalePDF = try model.reviewPDF(bytes: pdfBytes, filename: "fixture.pdf"); model.configure(nil); let beforePDFSwitch = KnowledgeWire.calls.count; check(await model.perform(stalePDF) == false && KnowledgeWire.calls.count == beforePDFSwitch, "disconnect revokes frozen PDF upload")
        model.configure(local)
        let manager = FileManager.default
        let folderURL = URL(fileURLWithPath: "/private/tmp", isDirectory: true).appendingPathComponent("feral-knowledge-folder-fixture-" + UUID().uuidString, isDirectory: true)
        try manager.createDirectory(at: folderURL, withIntermediateDirectories: true)
        defer { try? manager.removeItem(at: folderURL) }
        let source = folderURL.appendingPathComponent("fixture.py")
        try Data("print('disposable folder fixture')\n".utf8).write(to: source)
        try Data("ignored binary".utf8).write(to: folderURL.appendingPathComponent("ignored.bin"))
        let ignoredFolder = folderURL.appendingPathComponent("node_modules", isDirectory: true)
        try manager.createDirectory(at: ignoredFolder, withIntermediateDirectories: false)
        try Data("excluded cached content".utf8).write(to: ignoredFolder.appendingPathComponent("ignore.js"))
        let captured = try NativeKnowledgeModel.captureFolder(folderURL)
        check(captured.files.count == 1 && captured.files["fixture.py"]?.count == 64 && captured.skipped == 1, "folder extension and ignored-directory rules match backend")
        let folderCalls = KnowledgeWire.calls.count
        let folderReview = try model.reviewFolder(folderURL)
        check(KnowledgeWire.calls.count == folderCalls && folderReview.folder?.files == captured.files, "folder review captures exact manifest without dispatch")
        check(folderReview.scope.contains("rereads") && folderReview.scope.contains("external providers") && folderReview.scope.contains("Partial writes"), "folder review discloses backend reread and durable side effects")
        check(await model.perform(folderReview), "folder exact manifest receipt accepted")
        let folderPost = KnowledgeWire.calls.last!
        check(folderPost.0 == "/api/wiki/ingest/repo" && folderPost.2["expected_files"] as? [String: String] == captured.files && folderPost.2["max_files"] as? Int == 100 && folderPost.2["compile_after"] as? Bool == false, "actual backend path/manifest/count/compile contract")
        check((folderPost.2["extensions_filter"] as? [String])?.contains(".swift") == true && folderPost.2.count == 5, "complete explicit extension rules without hidden config replacement")
        let replayFolderCalls = KnowledgeWire.calls.count; check(await model.perform(folderReview) == false && KnowledgeWire.calls.count == replayFolderCalls, "folder review single use")
        let changedFolder = try model.reviewFolder(folderURL)
        try Data("changed after review".utf8).write(to: source)
        let beforeChanged = KnowledgeWire.calls.count
        check(await model.perform(changedFolder) == false && KnowledgeWire.calls.count == beforeChanged, "changed bytes fail local revalidation before POST")
        let wrongFolder = try model.reviewFolder(folderURL); KnowledgeWire.wrongFolderHash = true
        check(await model.perform(wrongFolder) == false && model.receipt == nil && model.error?.contains("may already") == true, "mismatched backend manifest cannot report success")
        KnowledgeWire.wrongFolderHash = false
        let revokedFolder = try model.reviewFolder(folderURL); model.discardReviews(); check(await model.perform(revokedFolder) == false, "dismissal releases folder capability")
        let symbolic = folderURL.appendingPathComponent("link.py")
        try manager.createSymbolicLink(at: symbolic, withDestinationURL: source)
        refuses("symlink file inside folder") { _ = try NativeKnowledgeModel.captureFolder(folderURL) }
        try manager.removeItem(at: symbolic)
        let ancestorLink = folderURL.appendingPathComponent("folder-link")
        try manager.createSymbolicLink(at: ancestorLink, withDestinationURL: ignoredFolder)
        refuses("selected folder is a symlink") { _ = try NativeKnowledgeModel.captureFolder(ancestorLink) }
        let actualPDF = ignoredFolder.appendingPathComponent("fixture.pdf")
        try pdfBytes.write(to: actualPDF)
        check(try NativeKnowledgeModel.capturePDF(actualPDF) == pdfBytes, "PDF capture freezes real regular-file bytes")
        refuses("PDF symlink ancestor") { _ = try NativeKnowledgeModel.capturePDF(ancestorLink.appendingPathComponent("fixture.pdf")) }
        let pdfLink = folderURL.appendingPathComponent("linked.pdf")
        try manager.createSymbolicLink(at: pdfLink, withDestinationURL: actualPDF)
        refuses("PDF selected file symlink") { _ = try NativeKnowledgeModel.capturePDF(pdfLink) }
        try manager.removeItem(at: pdfLink)
        try manager.removeItem(at: ancestorLink)
        try Data(repeating: 1, count: 80_001).write(to: source)
        refuses("supported file over 80000 bytes") { _ = try NativeKnowledgeModel.captureFolder(folderURL) }
        try Data("restored fixture".utf8).write(to: source)
        for i in 0..<100 { try Data("count fixture".utf8).write(to: folderURL.appendingPathComponent("count-\(i).txt")) }
        refuses("folder over 100 supported files") { _ = try NativeKnowledgeModel.captureFolder(folderURL) }
        print("Native knowledge feature: \(count) assertions passed")
    }
}
