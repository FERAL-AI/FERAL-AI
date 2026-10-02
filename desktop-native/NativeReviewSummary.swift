import SwiftUI
import Foundation

/// Presentation only. The owning feature keeps its original immutable review,
/// preflight, expiry, authorization and send controls. Never send this summary.
struct NativeReviewSummary: Equatable {
    struct Field: Equatable {
        let label: String
        let value: String
    }
    let action: String
    let effect: String
    /// Material privacy, cost and permission scope stays visible when collapsed.
    let materialScope: [String]
    /// Exact destination, device, merchant or amount must not be hidden in details.
    let targets: [Field]
    /// Original complete disclosure supplied by the owning feature, verbatim.
    /// No parsing, redaction, truncation or validation is performed here.
    let details: String

    var visibleSummaryText: String {
        ([action, effect] + materialScope + targets.map { $0.label + ": " + $0.value }).joined(separator: "\n")
    }
}

/// Embeds in the host's existing review sheet; adds no confirmation or action.
struct NativeReviewSummaryView: View {
    let review: NativeReviewSummary
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(verbatim: review.action).font(.title2.bold())
            Text(verbatim: review.effect)
            ForEach(Array(review.materialScope.enumerated()), id: \.offset) { entry in
                Text(verbatim: entry.element).font(.callout)
            }
            ForEach(Array(review.targets.enumerated()), id: \.offset) { entry in
                VStack(alignment: .leading, spacing: 3) {
                    Text(verbatim: entry.element.label).font(.caption).foregroundStyle(.secondary)
                    Text(verbatim: entry.element.value).textSelection(.enabled)
                }.accessibilityElement(children: .contain)
            }
            DisclosureGroup("Full scope and details", isExpanded: $expanded) {
                Text(verbatim: review.details)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, 6)
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Static, non-operational examples for design inspection; not adapters or grants.
enum NativeReviewSummaryExamples {
    static let existingVault = NativeReviewSummary(
        action: "Unlock your existing vault?",
        effect: "Makes saved credentials available and may restore local memory working data.",
        materialScope: ["macOS may ask for access to the existing Keychain key.", "An OS operation already started may continue after a timeout."],
        targets: [.init(label: "Local service", value: "http://127.0.0.1:9090"), .init(label: "Server-reviewed scope", value: "Example scope: access the existing vault key")],
        details: "Design example only. In a real review, inject the original complete vault explanation unchanged; retain expiry, readiness and server-token checks in the owning model."
    )
    static let providerActivation = NativeReviewSummary(
        action: "Use this provider for chat?",
        effect: "Saves the selected model and endpoint for chat. This does not verify a successful response.",
        materialScope: ["Activation can contact the provider or gateway. Existing fallback settings remain part of the reviewed configuration."],
        targets: [.init(label: "Provider / model", value: "ollama / example-model"), .init(label: "Provider endpoint", value: "http://127.0.0.1:11434"), .init(label: "Local service", value: "http://127.0.0.1:9090")],
        details: "Design example only. In a real review, inject the unchanged original activation explanation and exact fallback metadata. Coding and voice settings remain separate."
    )
}
