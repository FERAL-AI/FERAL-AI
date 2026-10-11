// Detect live-field layout mutation using only synthetic AppKit text.
import SwiftUI
import AppKit

@main struct SelectableMeasurementProbe {
    @MainActor static func main() {
        _ = NSApplication.shared
        let text = String(repeating: "Synthetic 👩🏽‍💻 café العربية 漢字 wrapping. ", count: 16)
        let field = NativeSelectableTextField.makeField(text: text)
        let view = NativeSelectableTextField(text: text, font: .systemFont(ofSize: 14), color: .labelColor, attributed: nil, wraps: true)
        view.configure(field)
        let originalWidth = field.preferredMaxLayoutWidth
        let a = view.fittingSize(width: 240, field: field)
        let widthAfterFirst = field.preferredMaxLayoutWidth
        let b = view.fittingSize(width: 720, field: field)
        let changed = originalWidth != widthAfterFirst || originalWidth != field.preferredMaxLayoutWidth
        print("measurement_changed_live_width=\(changed) before=\(originalWidth) first=\(widthAfterFirst) last=\(field.preferredMaxLayoutWidth) narrow_height=\(a.height) wide_height=\(b.height)")
        precondition(!changed, "A sizeThatFits measurement mutated the attached view's intrinsic-size input")
    }
}
