import Foundation
import SwiftUI
import CoreFoundation
import CryptoKit
import AppKit
import UniformTypeIdentifiers
import Darwin

private struct KnowledgeFailure: Error { let message: String }
private func knowledgeError(_ error: Error) -> String { (error as? KnowledgeFailure)?.message ?? "Request failed. Private backend details are withheld. Refresh before retrying." }
private func knowledgeInt(_ value: Any?) -> Int? { guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue == Double(number.intValue), number.intValue >= 0 else { return nil }; return number.intValue }
private func knowledgeTimestamp(_ value: Any?) throws -> String {
    if let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue.isFinite, number.doubleValue > 0, number.doubleValue <= 253_402_300_799 {
        return Date(timeIntervalSince1970: number.doubleValue).formatted()
    }
    // Accept strict ISO timestamps for interoperability. Never display arbitrary
    // strings or coerce boolean/unknown objects into a timestamp.
    if let string = value as? String, string.utf8.count <= 40 {
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = iso.date(from: string) { return date.formatted() }
        iso.formatOptions = [.withInternetDateTime]
        if let date = iso.date(from: string) { return date.formatted() }
    }
    throw KnowledgeFailure(message: "Knowledge page timestamp is malformed.")
}
final class NativeKnowledgeRedirectGuard: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
    static func session() -> URLSession { URLSession(configuration: .ephemeral, delegate: NativeKnowledgeRedirectGuard(), delegateQueue: nil) }
}
struct NativeKnowledgePage: Identifiable {
    let id: String, title: String, kind: String, body: String, updated: String
    let references: [String]
}
struct NativeKnowledgeReview: Identifiable {
    let id = UUID(), generation: UUID
    let title: String, scope: String, content: String, sourceLabel: String
    let compile: Bool
    var pdfBytes: Data? = nil
    var pdfSHA256: String? = nil
    var compileAfter = false
    var folder: NativeKnowledgeFolderSnapshot? = nil
}
struct NativeKnowledgeFolderSnapshot {
    let path: String
    let files: [String: String]
    let bytes: Int
    let skipped: Int
}
private enum KnowledgeFolderCapture {
    static let extensions = ["py", "js", "jsx", "ts", "tsx", "json", "yaml", "yml", "md", "txt", "sh", "toml", "ini", "cfg", "sql", "go", "rs", "java", "swift", "kt", "html", "css"]
    static let ignored: Set<String> = [".git", ".idea", ".vscode", "__pycache__", "node_modules", "dist", "build", ".next", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".cursor"]
    static func unchanged(_ before: stat, _ after: stat) -> Bool { before.st_dev == after.st_dev && before.st_ino == after.st_ino && before.st_size == after.st_size && before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec && before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec && before.st_ctimespec.tv_sec == after.st_ctimespec.tv_sec && before.st_ctimespec.tv_nsec == after.st_ctimespec.tv_nsec }
    static func whitespace(_ scalar: Unicode.Scalar) -> Bool {
        switch scalar.value { case 9...13, 28...32, 133, 160, 5760, 8192...8202, 8232, 8233, 8239, 8287, 12288: return true; default: return false }
    }
    static func capture(_ url: URL) throws -> NativeKnowledgeFolderSnapshot {
        guard url.isFileURL else { throw KnowledgeFailure(message: "Choose a local folder.") }
        // Foundation standardization can resolve a filesystem alias/symlink before
        // nofollow traversal sees it. Preserve the selected literal path instead.
        let parts = url.path.split(separator: "/")
        guard !parts.isEmpty, !parts.contains("."), !parts.contains("..") else { throw KnowledgeFailure(message: "Choose a specific absolute folder without relative path components.") }
        let path = "/" + parts.joined(separator: "/")
        var root = Darwin.open("/", O_RDONLY | O_DIRECTORY)
        guard root >= 0 else { throw KnowledgeFailure(message: "Could not open the selected folder.") }
        do {
            for component in path.split(separator: "/") {
                let next = Darwin.openat(root, String(component), O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
                guard next >= 0 else { throw KnowledgeFailure(message: "Folder ancestors must be readable directories without symlinks.") }
                Darwin.close(root); root = next
            }
        } catch { Darwin.close(root); throw error }
        defer { Darwin.close(root) }
        var entries = 0, total = 0, skipped = 0
        var manifest: [String: String] = [:]
        func walk(_ fd: Int32, prefix: String, depth: Int) throws {
            var before = stat(); guard fstat(fd, &before) == 0 else { throw KnowledgeFailure(message: "Could not inspect the selected folder.") }
            let copy = dup(fd); guard copy >= 0 else { throw KnowledgeFailure(message: "Could not enumerate the folder.") }
            guard let directory = fdopendir(copy) else { Darwin.close(copy); throw KnowledgeFailure(message: "Could not enumerate the folder.") }
            var names: [String] = []
            do {
                errno = 0
                while let item = readdir(directory) {
                    let name = withUnsafePointer(to: &item.pointee.d_name) { pointer in pointer.withMemoryRebound(to: CChar.self, capacity: 1024) { String(validatingUTF8: $0) } }
                    guard let name else { throw KnowledgeFailure(message: "Folder filenames must use UTF-8.") }
                    if name == "." || name == ".." { continue }
                    entries += 1; guard entries <= 2000 else { throw KnowledgeFailure(message: "Folder exceeds 2,000 traversed entries. Choose a smaller folder.") }
                    names.append(name); errno = 0
                }
                guard errno == 0 else { throw KnowledgeFailure(message: "Folder enumeration was incomplete.") }
                closedir(directory)
            } catch { closedir(directory); throw error }
            for name in names.sorted() {
                var meta = stat(); guard fstatat(fd, name, &meta, AT_SYMLINK_NOFOLLOW) == 0 else { throw KnowledgeFailure(message: "Folder changed during capture. Review again.") }
                let mode = meta.st_mode & mode_t(S_IFMT), relative = prefix + name
                if mode == mode_t(S_IFLNK) { throw KnowledgeFailure(message: "Folder contains symlinks. Choose a folder with regular files only.") }
                if mode == mode_t(S_IFDIR) {
                    if ignored.contains(name) { continue }
                    guard depth < 16 else { throw KnowledgeFailure(message: "Folder exceeds 16 nested directory levels.") }
                    let child = Darwin.openat(fd, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
                    guard child >= 0 else { throw KnowledgeFailure(message: "Folder changed or could not be opened.") }
                    do { try walk(child, prefix: relative + "/", depth: depth + 1); Darwin.close(child) } catch { Darwin.close(child); throw error }
                } else {
                    let suffix = name.lastIndex(of: ".").flatMap { $0 == name.startIndex ? nil : String(name[name.index(after: $0)...]).lowercased() }
                    guard mode == mode_t(S_IFREG), let suffix, extensions.contains(suffix) else { skipped += 1; continue }
                    guard manifest.count < 100 else { throw KnowledgeFailure(message: "Folder exceeds 100 supported files. Choose a smaller folder.") }
                    let file = Darwin.openat(fd, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
                    guard file >= 0 else { throw KnowledgeFailure(message: "A file changed or could not be read.") }
                    var bytes = Data()
                    do {
                        var start = stat(); guard fstat(file, &start) == 0, start.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG), start.st_size <= 80_000 else { throw KnowledgeFailure(message: "A supported file exceeds 80,000 bytes or is not regular.") }
                        var buffer = [UInt8](repeating: 0, count: 8192)
                        while true { let n = Darwin.read(file, &buffer, buffer.count); guard n >= 0 else { throw KnowledgeFailure(message: "Could not capture a file completely.") }; if n == 0 { break }; bytes.append(buffer, count: n); guard bytes.count <= 80_000 else { throw KnowledgeFailure(message: "A supported file exceeds 80,000 bytes.") } }
                        var end = stat(); guard fstat(file, &end) == 0, unchanged(start, end), bytes.count == Int(start.st_size) else { throw KnowledgeFailure(message: "A file changed during capture. Review again.") }
                        Darwin.close(file)
                    } catch { Darwin.close(file); throw error }
                    guard !bytes.contains(0), let text = String(data: bytes, encoding: .utf8), !text.unicodeScalars.allSatisfy(whitespace) else { skipped += 1; continue }
                    total += bytes.count; guard total <= 8 * 1024 * 1024 else { throw KnowledgeFailure(message: "Folder exceeds 8 MiB of supported text.") }
                    guard manifest[relative] == nil else { throw KnowledgeFailure(message: "Folder contains ambiguous equivalent filenames.") }
                    manifest[relative] = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
                }
            }
            var after = stat(); guard fstat(fd, &after) == 0, unchanged(before, after) else { throw KnowledgeFailure(message: "Folder changed during capture. Review again.") }
        }
        try walk(root, prefix: "", depth: 0)
        guard !manifest.isEmpty else { throw KnowledgeFailure(message: "Folder has no supported nonempty UTF-8 text files.") }
        return NativeKnowledgeFolderSnapshot(path: path, files: manifest, bytes: total, skipped: skipped)
    }
}
@MainActor final class NativeKnowledgeModel: ObservableObject {
    @Published private(set) var pages: [NativeKnowledgePage] = []
    @Published private(set) var page: NativeKnowledgePage?
    @Published private(set) var total: Int?
    @Published private(set) var kinds: [String] = []
    @Published private(set) var busy = false
    @Published private(set) var error: String?
    @Published private(set) var receipt: String?
    private var baseURL: URL?
    private var generation = UUID()
    private var readRevision = UUID()
    private var reviews: [UUID: NativeKnowledgeReview] = [:]
    private let session: URLSession
    init(baseURL: URL?, session: URLSession? = nil) { self.baseURL = baseURL; self.session = session ?? NativeKnowledgeRedirectGuard.session() }
    func configure(_ url: URL?) { guard url != baseURL else { return }; baseURL = url; generation = UUID(); readRevision = UUID(); reviews = [:]; pages = []; page = nil; total = nil; kinds = []; busy = false; error = nil; receipt = nil }
    private func request(_ path: String, query: [URLQueryItem] = [], body: [String: Any]? = nil, multipart: Data? = nil, boundary: String? = nil) async throws -> [String: Any] {
        try Task.checkCancellation()
        let start = generation
        guard let baseURL, ["http", "https"].contains(baseURL.scheme ?? ""), ["127.0.0.1", "::1", "[::1]"].contains(baseURL.host ?? ""), baseURL.user == nil, baseURL.password == nil, var parts = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else { throw KnowledgeFailure(message: "Connect to the app-owned loopback service.") }
        parts.path = path; parts.queryItems = query.isEmpty ? nil : query; parts.fragment = nil
        guard let url = parts.url else { throw KnowledgeFailure(message: "Invalid knowledge request.") }
        var request = URLRequest(url: url); request.timeoutInterval = 120
        if let body { request.httpMethod = "POST"; request.httpBody = try JSONSerialization.data(withJSONObject: body); request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        if let multipart, let boundary { request.httpMethod = "POST"; request.httpBody = multipart; request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type") }
        let (data, response) = try await session.data(for: request); try Task.checkCancellation()
        guard generation == start else { throw KnowledgeFailure(message: "Agent changed. Review again.") }
        guard let response = response as? HTTPURLResponse, (200..<300).contains(response.statusCode), data.count <= 4_194_304, let value = try JSONSerialization.jsonObject(with: data) as? [String: Any], value["error"] == nil else { throw KnowledgeFailure(message: "The backend refused the request or returned unreadable knowledge state.") }
        return value
    }
    private func parse(_ row: [String: Any], detail: Bool = false) throws -> NativeKnowledgePage {
        guard let id = row["id"] as? String, !id.isEmpty, id.utf8.count <= 512, let title = row["title"] as? String, let kind = row["kind"] as? String, let refs = row["source_refs"] as? [[String: Any]] else { throw KnowledgeFailure(message: "Knowledge page metadata is incomplete.") }
        let updated = try knowledgeTimestamp(row["updated_at"])
        if detail && !(row["body_markdown"] is String) { throw KnowledgeFailure(message: "Knowledge page content is unavailable.") }
        let rendered = try refs.map { String(decoding: try JSONSerialization.data(withJSONObject: $0, options: [.sortedKeys]), as: UTF8.self) }
        return NativeKnowledgePage(id: id, title: title, kind: kind, body: row["body_markdown"] as? String ?? "", updated: updated, references: rendered)
    }
    func refresh(query: String = "", kind: String = "") async {
        guard !busy else { return }; let start = generation; busy = true; defer { if generation == start { busy = false } }
        error = nil; page = nil; readRevision = UUID()
        do {
            guard query.utf8.count <= 4096, kind.utf8.count <= 512 else { throw KnowledgeFailure(message: "Search is too long.") }
            let value = try await request("/api/wiki/pages", query: [URLQueryItem(name: "q", value: query), URLQueryItem(name: "kind", value: kind), URLQueryItem(name: "limit", value: "100")])
            guard let rows = value["pages"] as? [[String: Any]], rows.count <= 100 else { throw KnowledgeFailure(message: "Page listing is incomplete or exceeds its limit.") }
            let parsed = try rows.map { try parse($0) }; guard Set(parsed.map(\.id)).count == parsed.count else { throw KnowledgeFailure(message: "Page listing contains duplicate identifiers.") }; pages = parsed
            let stats = try await request("/api/wiki/stats")
            guard let count = knowledgeInt(stats["pages"]), let counts = stats["kinds"] as? [[String: Any]], counts.allSatisfy({ $0["kind"] is String && knowledgeInt($0["count"]) != nil }) else { throw KnowledgeFailure(message: "Knowledge statistics are incomplete.") }
            total = count; kinds = Array(Set(counts.compactMap { $0["kind"] as? String })).sorted()
        } catch { if generation == start { self.error = knowledgeError(error); pages = []; total = nil; kinds = [] } }
    }
    func open(_ id: String) async {
        guard !busy, pages.contains(where: { $0.id == id }) else { return }; let revision = UUID(); readRevision = revision; let start = generation; page = nil; error = nil
        do {
            // Generated identifiers include internal dots; ambiguous or path-shaped IDs are refused.
            let safe = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"); guard id != ".", id != "..", let encoded = id.addingPercentEncoding(withAllowedCharacters: safe) else { throw KnowledgeFailure(message: "Invalid page identifier.") }
            let raw = try await requestEncodedPage(encoded)
            let parsed = try parse(raw, detail: true); guard parsed.id == id else { throw KnowledgeFailure(message: "The returned page identifier differs from the selected page.") }; if readRevision == revision && generation == start { page = parsed }
        } catch { if readRevision == revision && generation == start { self.error = knowledgeError(error) } }
    }
    private func requestEncodedPage(_ encoded: String) async throws -> [String: Any] {
        // Backend wiki IDs are simple generated identifiers. Reject path-shaped IDs instead of ambiguous routing.
        guard !encoded.contains("%") else { throw KnowledgeFailure(message: "This legacy page identifier cannot safely be opened by this client.") }
        return try await request("/api/wiki/pages/" + encoded)
    }
    func reviewText(content: String, sourceLabel: String) throws -> NativeKnowledgeReview {
        guard !busy, baseURL != nil else { throw KnowledgeFailure(message: "Wait for the connected agent.") }
        guard !content.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, content.utf8.count <= 65_536, !content.contains("\0"), sourceLabel.utf8.count <= 256, !sourceLabel.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw KnowledgeFailure(message: "Enter text up to 64 KiB and a source label up to 256 bytes.") }
        let review = NativeKnowledgeReview(generation: generation, title: "Import text into memory", scope: "The exact text below will be stored as durable chunked notes. Background embeddings may send it to configured external providers. This does not compile wiki pages, erase old notes, or guarantee deletion of derivatives. Repeating an import can create duplicate notes.", content: content, sourceLabel: sourceLabel, compile: false); reviews[review.id] = review; return review
    }
    func reviewCompile() throws -> NativeKnowledgeReview {
        guard !busy, baseURL != nil else { throw KnowledgeFailure(message: "Wait for the connected agent.") }
        let review = NativeKnowledgeReview(generation: generation, title: "Compile memory wiki", scope: "Rebuild derived pages from up to 200 notes, 200 episodes and 400 knowledge records, plus identity context. Compilation uses existing stored data. Old derived pages can remain; this is not a backup, complete rebuild, or erasure operation.", content: "", sourceLabel: "", compile: true); reviews[review.id] = review; return review
    }
    func reviewPDF(bytes: Data, filename: String, compileAfter: Bool = false) throws -> NativeKnowledgeReview {
        guard !busy, baseURL != nil else { throw KnowledgeFailure(message: "Wait for the connected agent.") }
        guard bytes.count > 0, bytes.count <= 10 * 1024 * 1024, bytes.starts(with: Data("%PDF-".utf8)), filename.lowercased().hasSuffix(".pdf"), filename.utf8.count <= 255, filename.utf8.allSatisfy({ $0 >= 32 && $0 != 127 && $0 != 34 && $0 != 47 && $0 != 92 }) else { throw KnowledgeFailure(message: "Choose a PDF up to 10 MiB with a valid filename and PDF header.") }
        let hash = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        let review = NativeKnowledgeReview(generation: generation, title: "Import PDF into memory", scope: "The frozen PDF bytes below will be uploaded to this app’s local service and extracted into durable notes. Background embeddings may send extracted text to configured external providers. Limits: 10 MiB, 100 pages, 1 MiB extracted text. Encrypted or unsupported files fail. Partial writes can remain after a failure; repeated imports may create duplicate notes. \(compileAfter ? "Wiki compilation is also requested; old derivatives may remain." : "Wiki compilation is not requested.")", content: "", sourceLabel: filename, compile: false, pdfBytes: bytes, pdfSHA256: hash, compileAfter: compileAfter)
        reviews[review.id] = review; return review
    }
    static func captureFolder(_ url: URL) throws -> NativeKnowledgeFolderSnapshot { try KnowledgeFolderCapture.capture(url) }
    func reviewFolder(_ url: URL, compileAfter: Bool = false) throws -> NativeKnowledgeReview {
        guard !busy, baseURL != nil else { throw KnowledgeFailure(message: "Wait for the connected agent.") }
        let folder = try Self.captureFolder(url)
        let review = NativeKnowledgeReview(generation: generation, title: "Import local folder into memory", scope: "The reviewed folder and exact supported-file hashes below will be checked again before dispatch. The backend then rereads these files and verifies the exact manifest before writing chunked notes; changes abort ingestion. Background embeddings may send file text to configured external providers. Limits: 100 files, 2,000 traversed entries, 16 nested levels, 80,000 bytes per file, 8 MiB aggregate text. Symlinks and uncertain traversal are rejected; ignored build/cache folders, unsupported files, empty files, binary and invalid UTF-8 files are excluded. Partial writes can remain if storage fails. Repeated imports may create duplicates. \(compileAfter ? "Wiki compilation is requested; old derivatives may remain." : "Wiki compilation is not requested.")", content: "", sourceLabel: folder.path, compile: false, compileAfter: compileAfter, folder: folder)
        reviews[review.id] = review; return review
    }
    static func capturePDF(_ url: URL) throws -> Data {
        guard url.isFileURL else { throw KnowledgeFailure(message: "Choose a local PDF file.") }
        let components = url.path.split(separator: "/")
        guard let filename = components.last, !components.contains("."), !components.contains("..") else { throw KnowledgeFailure(message: "Choose a file with an absolute unambiguous path.") }
        var parent = Darwin.open("/", O_RDONLY | O_DIRECTORY)
        guard parent >= 0 else { throw KnowledgeFailure(message: "Could not open the selected file’s directory.") }
        do {
            for component in components.dropLast() {
                let child = Darwin.openat(parent, String(component), O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
                guard child >= 0 else { throw KnowledgeFailure(message: "PDF ancestors must be readable directories without symlinks.") }
                Darwin.close(parent); parent = child
            }
        } catch { Darwin.close(parent); throw error }
        let fd = Darwin.openat(parent, String(filename), O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        Darwin.close(parent)
        guard fd >= 0 else { throw KnowledgeFailure(message: "Could not open the selected regular file.") }; defer { Darwin.close(fd) }
        var before = stat(); guard fstat(fd, &before) == 0, before.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG), before.st_size > 0, before.st_size <= 10 * 1024 * 1024 else { throw KnowledgeFailure(message: "Choose a regular PDF up to 10 MiB.") }
        var bytes = Data(); var buffer = [UInt8](repeating: 0, count: 65536)
        while true { let n = Darwin.read(fd, &buffer, buffer.count); guard n >= 0 else { throw KnowledgeFailure(message: "Could not read the selected PDF.") }; if n == 0 { break }; bytes.append(buffer, count: n); guard bytes.count <= 10 * 1024 * 1024 else { throw KnowledgeFailure(message: "PDF exceeds 10 MiB.") } }
        var after = stat(); guard fstat(fd, &after) == 0, before.st_dev == after.st_dev, before.st_ino == after.st_ino, before.st_size == after.st_size, before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec, before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec, before.st_ctimespec.tv_sec == after.st_ctimespec.tv_sec, before.st_ctimespec.tv_nsec == after.st_ctimespec.tv_nsec, bytes.count == Int(before.st_size) else { throw KnowledgeFailure(message: "PDF changed while being captured. Choose it again.") }
        return bytes
    }
    private func pdfMultipart(_ review: NativeKnowledgeReview, boundary: String) -> Data {
        var result = Data()
        func append(_ value: String) { result.append(Data(value.utf8)) }
        for (key, value) in [("expected_sha256", review.pdfSHA256 ?? ""), ("compile_after", review.compileAfter ? "true" : "false")] { append("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(key)\"\r\n\r\n\(value)\r\n") }
        append("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(review.sourceLabel)\"\r\nContent-Type: application/pdf\r\n\r\n")
        result.append(review.pdfBytes ?? Data()); append("\r\n--\(boundary)--\r\n"); return result
    }
    func cancel(_ review: NativeKnowledgeReview) { reviews[review.id] = nil }
    func discardReviews() { reviews = [:] }
    func perform(_ review: NativeKnowledgeReview) async -> Bool {
        guard !busy, review.generation == generation, let held = reviews.removeValue(forKey: review.id) else { return false }
        let review = held
        let start = generation; busy = true; error = nil; receipt = nil; readRevision = UUID(); defer { if generation == start { busy = false } }
        do {
            if let folder = review.folder {
                let current = try Self.captureFolder(URL(fileURLWithPath: folder.path, isDirectory: true))
                let expected = try JSONSerialization.data(withJSONObject: folder.files, options: [.sortedKeys])
                guard try JSONSerialization.data(withJSONObject: current.files, options: [.sortedKeys]) == expected, current.bytes == folder.bytes else { throw KnowledgeFailure(message: "Folder no longer matches the reviewed manifest. Review again.") }
                let result = try await request("/api/wiki/ingest/repo", body: ["path": folder.path, "extensions_filter": KnowledgeFolderCapture.extensions.map { "." + $0 }, "max_files": 100, "expected_files": folder.files, "compile_after": review.compileAfter])
                guard let ok = result["ok"] as? NSNumber, CFGetTypeID(ok) == CFBooleanGetTypeID(), ok.boolValue, result["source"] as? String == "repo", result["path"] as? String == folder.path, let actual = result["snapshot_files"] as? [String: String], try JSONSerialization.data(withJSONObject: actual, options: [.sortedKeys]) == expected, knowledgeInt(result["files_processed"]) == folder.files.count, let count = knowledgeInt(result["notes_saved"]), count > 0, let compile = result["compile"] as? [String: Any], let compiled = compile["compiled"] as? NSNumber, CFGetTypeID(compiled) == CFBooleanGetTypeID(), compiled.boolValue == review.compileAfter else { throw KnowledgeFailure(message: "Folder receipt does not confirm the reviewed manifest. Notes may already have been stored; inspect memory before retrying.") }
                receipt = "Backend confirmed \(folder.files.count) reviewed files, \(folder.bytes) bytes and \(count) stored notes. Background embeddings may still run."
            } else if let bytes = review.pdfBytes, let hash = review.pdfSHA256 {
                let boundary = "FERALWiki-" + UUID().uuidString
                let result = try await request("/api/wiki/ingest/pdf", multipart: pdfMultipart(review, boundary: boundary), boundary: boundary)
                guard let ok = result["ok"] as? NSNumber, CFGetTypeID(ok) == CFBooleanGetTypeID(), ok.boolValue, result["source"] as? String == "pdf", result["snapshot_sha256"] as? String == hash, let count = knowledgeInt(result["notes_saved"]), count > 0, let pages = knowledgeInt(result["pages_read"]), pages > 0, pages <= 100, let compile = result["compile"] as? [String: Any], let compiled = compile["compiled"] as? NSNumber, CFGetTypeID(compiled) == CFBooleanGetTypeID(), compiled.boolValue == review.compileAfter else { throw KnowledgeFailure(message: "PDF receipt does not confirm the reviewed snapshot. Notes may already have been stored; inspect memory before retrying.") }
                receipt = "Backend confirmed PDF hash \(hash), \(bytes.count) bytes, \(pages) text pages and \(count) notes. Background embeddings may still run."
            } else if review.compile {
                let result = try await request("/api/wiki/compile", body: ["notes_limit": 200, "episodes_limit": 200, "knowledge_limit": 400])
                guard let compiled = result["compiled"] as? NSNumber, CFGetTypeID(compiled) == CFBooleanGetTypeID(), compiled.boolValue, let total = knowledgeInt(result["total_pages"]) else { throw KnowledgeFailure(message: "Compilation receipt is incomplete. Changes may already have occurred; inspect pages before retrying.") }
                receipt = "Compilation reported complete: \(total) total pages. Old derivatives may remain."
            } else {
                let result = try await request("/api/wiki/ingest/text", body: ["content": review.content, "source_label": review.sourceLabel, "compile_after": false])
                guard let ok = result["ok"] as? NSNumber, CFGetTypeID(ok) == CFBooleanGetTypeID(), ok.boolValue, result["source"] as? String == "text", result["source_label"] as? String == review.sourceLabel, let count = knowledgeInt(result["notes_saved"]), count > 0, let compile = result["compile"] as? [String: Any], let compiled = compile["compiled"] as? NSNumber, CFGetTypeID(compiled) == CFBooleanGetTypeID(), !compiled.boolValue else { throw KnowledgeFailure(message: "Import receipt is incomplete. Notes may already have been stored; inspect memory before retrying.") }
                receipt = "Backend reported \(count) notes stored. Wiki compilation was not requested; background embeddings may still run."
            }
            reviews = [:]; return true
        } catch { if generation == start { self.error = knowledgeError(error) + " A write may already have occurred; do not retry blindly." }; return false }
    }
}

struct NativeKnowledgeFeatureView: View {
    let baseURL: URL?
    @StateObject private var model: NativeKnowledgeModel
    @State private var query = ""
    @State private var kind = ""
    @State private var content = ""
    @State private var label = "native-manual"
    @State private var review: NativeKnowledgeReview?
    @State private var localError: String?
    @State private var pdfCompileAfter = false
    init(baseURL: URL?) { self.baseURL = baseURL; _model = StateObject(wrappedValue: NativeKnowledgeModel(baseURL: baseURL)) }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { Text("Knowledge wiki").font(.title2.bold()); Spacer(); if model.busy { ProgressView().controlSize(.small) }; Button("Refresh") { search() }.disabled(model.busy) }
            Text("Read compiled memory pages and their source references. Page editing and deletion are unavailable in the current backend.").foregroundStyle(.secondary)
            HStack { TextField("Search pages", text: $query).onSubmit { search() }; Picker("Kind", selection: $kind) { Text("All").tag(""); ForEach(model.kinds, id: \.self) { Text($0).tag($0) } }.frame(width: 180); Button("Search") { search() }.disabled(model.busy) }
            if let count = model.total { Text("\(count) stored pages · showing up to 100 matches").font(.caption) }
            if let message = localError ?? model.error { Text(message).foregroundStyle(.red).textSelection(.enabled) }
            if let receipt = model.receipt { Text(receipt).foregroundStyle(.secondary).textSelection(.enabled) }
            HSplitView {
                ScrollView { LazyVStack(alignment: .leading) { ForEach(model.pages) { item in Button { Task { await model.open(item.id) } } label: { VStack(alignment: .leading) { Text(item.title).font(.headline); Text("\(item.kind) · \(item.updated)").font(.caption).foregroundStyle(.secondary) }.frame(maxWidth: .infinity, alignment: .leading).padding(8) }.buttonStyle(.plain) } } }.frame(minWidth: 180, idealWidth: 250)
                ScrollView { if let page = model.page { VStack(alignment: .leading, spacing: 10) { Text(page.title).font(.title3.bold()); Text(page.body).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading); Divider(); Text("Source references").font(.headline); ForEach(Array(page.references.enumerated()), id: \.offset) { _, ref in Text(ref).font(.system(.caption, design: .monospaced)).textSelection(.enabled) }; if page.references.isEmpty { Text("No references reported.").foregroundStyle(.secondary) } }.padding(8) } else { Text("Select a page to read its stored content.").foregroundStyle(.secondary).padding() } }.frame(minWidth: 300)
            }.frame(minHeight: 220)
            DisclosureGroup("Import text into memory") {
                TextField("Source label", text: $label)
                NativePlainTextEditor(text: $content, label: "Knowledge import text").frame(height: 130)
                Text("Up to 64 KiB. Imports can invoke configured embedding providers in the background.").font(.caption).foregroundStyle(.secondary)
                Button("Review exact text import…") { prepare { try model.reviewText(content: content, sourceLabel: label) } }.disabled(model.busy)
            }
            HStack { Button("Review wiki compilation…") { prepare { try model.reviewCompile() } }.disabled(model.busy); Spacer() }
            HStack { Toggle("Compile after file import", isOn: $pdfCompileAfter).toggleStyle(.checkbox); Spacer(); Button("Choose PDF to review…") { choosePDF() }.disabled(model.busy); Button("Choose folder to review…") { chooseFolder() }.disabled(model.busy) }
            Text("Folder ingestion reads supported local text files. It does not clone a remote repository.").font(.caption).foregroundStyle(.secondary)
        }.padding().task(id: baseURL) { review = nil; model.configure(baseURL); await model.refresh() }
        .sheet(item: $review, onDismiss: { model.discardReviews() }) { item in reviewPanel(item) }
    }
    private func search() { Task { await model.refresh(query: query, kind: kind) } }
    private func prepare(_ make: () throws -> NativeKnowledgeReview) { do { localError = nil; review = try make() } catch { localError = knowledgeError(error) } }
    private func choosePDF() {
        let panel = NSOpenPanel(); panel.allowedContentTypes = [.pdf]; panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let scope = url.startAccessingSecurityScopedResource(); defer { if scope { url.stopAccessingSecurityScopedResource() } }
        prepare { try model.reviewPDF(bytes: NativeKnowledgeModel.capturePDF(url), filename: url.lastPathComponent, compileAfter: pdfCompileAfter) }
    }
    private func chooseFolder() {
        let panel = NSOpenPanel(); panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let scope = url.startAccessingSecurityScopedResource(); defer { if scope { url.stopAccessingSecurityScopedResource() } }
        prepare { try model.reviewFolder(url, compileAfter: pdfCompileAfter) }
    }
    private func reviewPanel(_ item: NativeKnowledgeReview) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(item.title).font(.title2.bold())
            Text(item.scope).textSelection(.enabled)
            if !item.compile {
                Text("Source: \(item.sourceLabel)")
                if let folder = item.folder {
                    Text("\(folder.files.count) supported files · \(folder.bytes) bytes · \(folder.skipped) skipped entries").font(.caption)
                    ScrollView { VStack(alignment: .leading, spacing: 8) { ForEach(folder.files.keys.sorted(), id: \.self) { path in Text("\(path)\nSHA-256: \(folder.files[path] ?? "")").font(.system(.caption, design: .monospaced)).textSelection(.enabled) } } }.frame(height: 250)
                } else if let bytes = item.pdfBytes, let hash = item.pdfSHA256 {
                    Text("Frozen size: \(bytes.count) bytes\nSHA-256: \(hash)").font(.system(.body, design: .monospaced)).textSelection(.enabled)
                } else {
                    ScrollView { Text(item.content).font(.system(.body, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }.frame(height: 250)
                }
            }
            if model.busy { Text("Import in progress. Closing the window cannot undo notes already written.").foregroundStyle(.secondary); ProgressView() }
            HStack {
                Button("Cancel") { model.cancel(item); review = nil }.disabled(model.busy)
                Spacer()
                Button("Confirm") { Task { let success = await model.perform(item); if success && item.pdfBytes == nil && item.folder == nil && !item.compile { content = "" }; review = nil } }.disabled(model.busy)
            }
        }.padding(24).frame(width: 620).interactiveDismissDisabled(model.busy)
    }
}
