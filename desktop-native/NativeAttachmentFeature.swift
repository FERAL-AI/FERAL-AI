import Foundation
import UniformTypeIdentifiers
import CoreFoundation
import CryptoKit

final class NativeLocalSessionDelegate: NSObject, URLSessionTaskDelegate, URLSessionWebSocketDelegate {
    var onSocketOpen: ((URLSessionWebSocketTask) -> Void)?
    var onSocketClose: ((URLSessionWebSocketTask) -> Void)?
    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didOpenWithProtocol negotiatedProtocol: String?) {
        onSocketOpen?(webSocketTask)
    }
    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        onSocketClose?(webSocketTask)
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        // The owned loopback API has no redirect contract. Never forward selected files or credentials elsewhere.
        completionHandler(nil)
    }
}

struct NativeAttachmentRef: Identifiable {
    let id: String
    let filename: String
    let contentType: String
    let sizeBytes: Int
    let sha256: String
    var record: [String: Any] {
        ["upload_id": id, "filename": filename, "content_type": contentType, "size_bytes": sizeBytes, "sha256": sha256]
    }
}

enum NativeAttachmentWire {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    static func receipt(_ object: [String: Any], expectedSize: Int) throws -> NativeAttachmentRef {
        guard let id = object["upload_id"] as? String, !id.isEmpty,
              let filename = object["filename"] as? String, !filename.isEmpty,
              let mime = object["content_type"] as? String, !mime.isEmpty,
              let size = object["size_bytes"] as? NSNumber, CFGetTypeID(size) != CFBooleanGetTypeID(),
              size.doubleValue == Double(expectedSize), expectedSize > 0,
              let hash = object["sha256"] as? String, hash.count == 64,
              hash.allSatisfy({ "0123456789abcdef".contains($0) }) else {
            throw Failure(message: "The agent did not confirm the complete attachment.")
        }
        return NativeAttachmentRef(id: id, filename: filename, contentType: mime, sizeBytes: expectedSize, sha256: hash)
    }
    static func multipart(data: Data, filename: String, contentType: String, boundary: String) -> Data {
        // Never allow a selected filename to inject multipart headers.
        let safeName = filename.replacingOccurrences(of: "\r", with: "_").replacingOccurrences(of: "\n", with: "_")
            .replacingOccurrences(of: "\"", with: "_").replacingOccurrences(of: "\\", with: "_")
        var body = Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(safeName)\"\r\nContent-Type: \(contentType)\r\n\r\n".utf8)
        body.append(data); body.append(Data("\r\n--\(boundary)--\r\n".utf8)); return body
    }
    static func upload(_ file: URL, baseURL: URL, session: URLSession) async throws -> NativeAttachmentRef {
        guard ["http", "https"].contains(baseURL.scheme ?? ""),
              ["127.0.0.1", "::1", "[::1]"].contains(baseURL.host ?? ""), baseURL.user == nil, baseURL.password == nil else {
            throw Failure(message: "Attachments require the local agent connection.")
        }
        let scoped = file.startAccessingSecurityScopedResource()
        defer { if scoped { file.stopAccessingSecurityScopedResource() } }
        let values = try file.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey])
        guard values.isRegularFile == true, let count = values.fileSize, count > 0, count <= 25 * 1024 * 1024 else {
            throw Failure(message: "Select a nonempty file up to 25 MiB. The agent also enforces its storage quota.")
        }
        let bytes = try Data(contentsOf: file)
        guard bytes.count == count else { throw Failure(message: "The file changed while being read. Select it again.") }
        let mime = UTType(filenameExtension: file.pathExtension)?.preferredMIMEType ?? "application/octet-stream"
        let boundary = "Feral-" + UUID().uuidString
        var request = URLRequest(url: baseURL.appendingPathComponent("api/uploads"))
        request.httpMethod = "POST"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.httpBody = multipart(data: bytes, filename: file.lastPathComponent, contentType: mime, boundary: boundary)
        let (data, response) = try await session.feralLocalData(for: request)
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let http = response as? HTTPURLResponse else { throw Failure(message: "The attachment response was unreadable.") }
        guard (200..<300).contains(http.statusCode) else {
            throw Failure(message: object["detail"] as? String ?? "The attachment could not be stored.")
        }
        let result = try receipt(object, expectedSize: bytes.count)
        let digest = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        guard result.sha256 == digest else { throw Failure(message: "The attachment checksum did not match. It was not added to the message.") }
        return result
    }
}
