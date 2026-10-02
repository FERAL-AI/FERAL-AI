import AppKit
import SwiftUI

@main struct NativeSelectableTextTests {
    @MainActor static func main() {
        _ = NSApplication.shared
        let font = NSFont.systemFont(ofSize: 14)
        let markdown = nativeMarkdownAttributedText("**Bold** *Italic* `code` [Public link](https://example.com)", font: font)
        precondition(markdown.string == "Bold Italic code Public link")
        let bold = markdown.attribute(.font, at: 0, effectiveRange: nil) as! NSFont
        let italic = markdown.attribute(.font, at: 5, effectiveRange: nil) as! NSFont
        precondition(NSFontManager.shared.traits(of: bold).contains(.boldFontMask))
        precondition(NSFontManager.shared.traits(of: italic).contains(.italicFontMask))
        precondition(markdown.attribute(.link, at: 17, effectiveRange: nil) as? URL == URL(string: "https://example.com"))
        let text = String(repeating: "Synthetic wrapping text. ", count: 20)
        let value = NativeSelectableTextField(text: text, font: font, color: .secondaryLabelColor, attributed: nil, wraps: true)
        let field = NativeSelectableTextField.makeField(text: text)
        value.configure(field)
        precondition(field.isSelectable && !field.isEditable && field.allowsEditingTextAttributes)
        precondition(field.stringValue == text && field.textColor == .secondaryLabelColor)
        let narrow = value.fittingSize(width: 140, field: field)
        let wide = value.fittingSize(width: 700, field: field)
        precondition(narrow.height > wide.height && wide.height > 0)
        let code = NativeSelectableTextField(text: text, font: .monospacedSystemFont(ofSize: 14, weight: .regular), color: .labelColor, attributed: nil, wraps: false)
        code.configure(field)
        precondition(code.fittingSize(width: 140, field: field).width > 140)
        let rich = NativeSelectableTextField(text: markdown.string, font: font, color: .secondaryLabelColor, attributed: markdown, wraps: true)
        rich.configure(field)
        let preservedBold = field.attributedStringValue.attribute(.font, at: 0, effectiveRange: nil) as! NSFont
        precondition(NSFontManager.shared.traits(of: preservedBold).contains(.boldFontMask))
        precondition(field.attributedStringValue.attribute(.link, at: 17, effectiveRange: nil) as? URL == URL(string: "https://example.com"))
        print("PASS native selectable text: read-only selection, dynamic wrapping, unwrapped code, secondary style, Markdown bold/italic/link retention. Actual link activation requires GUI verification.")
    }
}
