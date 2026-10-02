import SwiftUI
import AppKit

struct NativeRichText: View {
    let text: String
    private struct Block {
        let text: String
        let language: String?
    }
    private var blocks: [Block] {
        var result: [Block] = []; var lines: [String] = []; var language: String?
        for line in text.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") {
                if let current = language {
                    result.append(Block(text: lines.joined(separator: "\n"), language: current)); lines = []; language = nil
                } else {
                    if !lines.isEmpty { result.append(Block(text: lines.joined(separator: "\n"), language: nil)); lines = [] }
                    language = String(trimmed.dropFirst(3))
                }
            } else { lines.append(line) }
        }
        if !lines.isEmpty { result.append(Block(text: lines.joined(separator: "\n"), language: language)) }
        return result
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                if let language = block.language {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            Text(language.isEmpty ? "Code" : language).font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            Button("Copy code") { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(block.text, forType: .string) }.font(.caption)
                        }
                        ScrollView(.horizontal) { Text(block.text).font(.system(.body, design: .monospaced)).textSelection(.enabled) }
                    }.padding(14).background(Color.secondary.opacity(0.07)).clipShape(RoundedRectangle(cornerRadius: 10))
                } else {
                    Text((try? AttributedString(markdown: block.text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(block.text))
                        .font(.body).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}
