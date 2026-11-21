import Foundation

@main struct NativeReviewSummaryTests {
    static func require(_ condition: Bool, _ name: String) {
        guard condition else { fatalError("FAIL: " + name) }
    }
    static func main() {
        // A collapsed review must still disclose concrete privacy/cost and target.
        let details = "Exact scope\n{\"action_id\":\"app_event\",\"session\":\"owner-123\"}\n" + String(repeating: "x", count: 131072)
        let review = NativeReviewSummary(action: "Send this app event?", effect: "Sends the entered value to the agent.", materialScope: ["Uses shared memory and tools.", "Can spend configured provider budget."], targets: [.init(label: "Service", value: "http://127.0.0.1:9090"), .init(label: "App / session", value: "fixture / owner-123")], details: details)
        require(review.visibleSummaryText.contains("shared memory and tools") && review.visibleSummaryText.contains("provider budget"), "material privacy and cost stay in collapsed summary")
        require(review.visibleSummaryText.contains("http://127.0.0.1:9090") && review.visibleSummaryText.contains("fixture / owner-123"), "exact owning destination remains visible")
        require(review.details == details && review.details.utf8.count > 131072, "expanded original authoritative metadata remains complete without truncation")
        let amount = "USD 19.99", merchant = "Example Merchant", device = "device-exact-001"
        let fields = [NativeReviewSummary.Field(label: "Amount", value: amount), .init(label: "Merchant", value: merchant), .init(label: "Device", value: device), .init(label: "Scope", value: "first"), .init(label: "Scope", value: "second")]
        let inspect = NativeReviewSummary(action: "Inspection example", effect: "No operation is connected.", materialScope: [], targets: fields, details: "[not a link](https://example.test)\n**not parsed**\n🦦 exact Unicode")
        require(inspect.targets == fields && inspect.visibleSummaryText.contains(amount) && inspect.visibleSummaryText.contains(merchant) && inspect.visibleSummaryText.contains(device), "amount merchant device and duplicate labels are preserved")
        require(inspect.details == "[not a link](https://example.test)\n**not parsed**\n🦦 exact Unicode", "presentation does not interpret metadata markup or alter Unicode")
        require(NativeReviewSummaryExamples.existingVault.materialScope.contains(where: { $0.contains("continue") }), "vault example keeps OS continuation consequence prominent")
        require(NativeReviewSummaryExamples.providerActivation.effect.contains("does not verify"), "activation example does not imply working inference")
        print("NativeReviewSummary: 7 checks passed")
    }
}
