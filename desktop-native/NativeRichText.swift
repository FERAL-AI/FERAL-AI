import SwiftUI
import AppKit

/// Explicit AppKit styles keep selection readable without relying on opaque
/// SwiftUI Font values or selectable SwiftUI accessibility nodes.
struct NativeSelectableFont {
    var value: NSFont
    static let body = Self(value: .preferredFont(forTextStyle: .body))
    static let callout = Self(value: .preferredFont(forTextStyle: .callout))
    static let caption = Self(value: .preferredFont(forTextStyle: .caption1))
    static let headline = Self(value: .preferredFont(forTextStyle: .headline))
    static let title = Self(value: .preferredFont(forTextStyle: .title1))
    static let title2 = Self(value: .preferredFont(forTextStyle: .title2))
    static let title3 = Self(value: .preferredFont(forTextStyle: .title3))
    static let largeTitle = Self(value: .preferredFont(forTextStyle: .largeTitle))
    static func system(_ style: Self, design: NSFontDescriptor.SystemDesign = .default) -> Self {
        guard let descriptor = style.value.fontDescriptor.withDesign(design),
              let font = NSFont(descriptor: descriptor, size: style.value.pointSize) else { return style }
        return Self(value: font)
    }
    func weight(_ weight: NSFont.Weight) -> Self {
        Self(value: .systemFont(ofSize: value.pointSize, weight: weight))
    }
    func bold() -> Self { Self(value: NSFontManager.shared.convert(value, toHaveTrait: .boldFontMask)) }
    func monospaced() -> Self { Self.system(self, design: .monospaced) }
}

/// AppKit owns selection and accessibility for read-only text. SwiftUI selectable
/// Text can recursively resolve its AX label on macOS 27 (see acceptance evidence).
struct NativeSelectableText: View {
    let text: String
    var font: NSFont = .systemFont(ofSize: NSFont.systemFontSize)
    var color: NSColor = .labelColor
    var attributed: NSAttributedString? = nil
    var wraps = true
    var maximumNumberOfLines = 0
    var label: String? = nil

    init(text: String, font: NSFont = .systemFont(ofSize: NSFont.systemFontSize), color: NSColor = .labelColor,
         attributed: NSAttributedString? = nil, wraps: Bool = true, maximumNumberOfLines: Int = 0) {
        self.text = text; self.font = font; self.color = color
        self.attributed = attributed; self.wraps = wraps; self.maximumNumberOfLines = maximumNumberOfLines
    }

    init(_ text: String) { self.init(text: text) }

    init(verbatim text: String) { self.init(text: text) }

    func font(_ style: NativeSelectableFont) -> Self {
        var copy = self; copy.font = style.value
        return copy
    }

    func foregroundStyle(_ color: Color) -> Self {
        var copy = self; copy.color = NSColor(color); return copy
    }

    func foregroundColor(_ color: Color?) -> Self {
        var copy = self; copy.color = color.map(NSColor.init) ?? .labelColor; return copy
    }

    func lineLimit(_ count: Int?) -> Self {
        var copy = self; copy.maximumNumberOfLines = max(0, count ?? 0); return copy
    }

    func accessibilityLabel(_ label: String) -> Self {
        var copy = self; copy.label = label; return copy
    }

    var body: some View {
        NativeSelectableTextField(text: text, font: font, color: color, attributed: attributed, wraps: wraps, maximumNumberOfLines: maximumNumberOfLines, label: label)
            .contextMenu {
                Button("Copy text") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(text, forType: .string)
                }
            }
    }
}

private final class NativeSelectableField: NSTextField {
    var usesAttributedValue = false
}

struct NativeSelectableTextField: NSViewRepresentable {
    let text: String
    let font: NSFont
    let color: NSColor
    let attributed: NSAttributedString?
    let wraps: Bool
    var maximumNumberOfLines = 0
    var label: String? = nil

    func makeNSView(context: Context) -> NSTextField {
        Self.makeField(text: text)
    }

    static func makeField(text: String) -> NSTextField {
        let field = NativeSelectableField(wrappingLabelWithString: text)
        field.isSelectable = true
        field.isEditable = false
        field.allowsEditingTextAttributes = true
        field.isBordered = false
        field.drawsBackground = false
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return field
    }

    func updateNSView(_ field: NSTextField, context: Context) {
        configure(field)
    }

    func configure(_ field: NSTextField) {
        if field.font != font { field.font = font }
        if field.textColor != color { field.textColor = color }
        if field.maximumNumberOfLines != maximumNumberOfLines { field.maximumNumberOfLines = maximumNumberOfLines }
        let breakMode: NSLineBreakMode = wraps ? .byWordWrapping : .byClipping
        if field.lineBreakMode != breakMode { field.lineBreakMode = breakMode }
        if field.cell?.wraps != wraps { field.cell?.wraps = wraps }
        if field.cell?.isScrollable != false { field.cell?.isScrollable = false }
        if let attributed {
            let styled = NSMutableAttributedString(attributedString: attributed)
            let full = NSRange(location: 0, length: styled.length)
            styled.addAttribute(.foregroundColor, value: color, range: full)
            styled.enumerateAttribute(.font, in: full) { existing, range, _ in
                if existing == nil { styled.addAttribute(.font, value: font, range: range) }
            }
            if !field.attributedStringValue.isEqual(to: styled) { field.attributedStringValue = styled }
            (field as? NativeSelectableField)?.usesAttributedValue = true
        } else {
            let wasAttributed = (field as? NativeSelectableField)?.usesAttributedValue ?? true
            if wasAttributed || field.stringValue != text { field.stringValue = text }
            (field as? NativeSelectableField)?.usesAttributedValue = false
        }
        let accessible = label ?? field.stringValue
        if field.accessibilityLabel() != accessible { field.setAccessibilityLabel(accessible) }
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView field: NSTextField, context: Context) -> CGSize? {
        fittingSize(width: proposal.width, field: field)
    }

    func fittingSize(width proposedWidth: CGFloat?, field: NSTextField) -> CGSize {
        // Measuring an attached field must not invalidate its intrinsic size.
        // Updating preferredMaxLayoutWidth here re-enters AppKit constraints.
        guard let cell = field.cell?.copy() as? NSTextFieldCell else { return .zero }
        let proposed = proposedWidth.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
        let natural = cell.cellSize.width
        let width = max(1, wraps ? (proposed ?? 600) : max(proposed ?? 0, natural.isFinite ? natural : 600))
        let size = cell.cellSize(forBounds: NSRect(x: 0, y: 0, width: width, height: .greatestFiniteMagnitude))
        let height = size.height.isFinite ? size.height : font.pointSize
        return CGSize(width: width, height: max(1, ceil(height)))
    }
}

func nativeMarkdownAttributedText(_ text: String, font: NSFont = .systemFont(ofSize: NSFont.systemFontSize)) -> NSAttributedString {
    guard let parsed = try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)) else { return NSAttributedString(string: text, attributes: [.font: font]) }
    let result = NSMutableAttributedString()
    for run in parsed.runs {
        var styledFont = font
        let intent = run.inlinePresentationIntent ?? []
        if intent.contains(.code) { styledFont = .monospacedSystemFont(ofSize: font.pointSize, weight: .regular) }
        if intent.contains(.stronglyEmphasized) { styledFont = NSFontManager.shared.convert(styledFont, toHaveTrait: .boldFontMask) }
        if intent.contains(.emphasized) { styledFont = NSFontManager.shared.convert(styledFont, toHaveTrait: .italicFontMask) }
        var attributes: [NSAttributedString.Key: Any] = [.font: styledFont]
        if let link = run.link { attributes[.link] = link }
        result.append(NSAttributedString(string: String(parsed[run.range].characters), attributes: attributes))
    }
    return result
}

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
                        ScrollView(.horizontal) { NativeSelectableText(text: block.text, font: .monospacedSystemFont(ofSize: NSFont.systemFontSize, weight: .regular), wraps: false) }
                    }.padding(14).background(Color.secondary.opacity(0.07)).clipShape(RoundedRectangle(cornerRadius: 10))
                } else {
                    NativeSelectableText(text: block.text, attributed: nativeMarkdownAttributedText(block.text))
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}
