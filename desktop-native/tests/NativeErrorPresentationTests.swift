import Foundation

@main struct NativeErrorPresentationTests {
    static func main() {
        func check(_ value: Bool, _ message: String) {
            guard value else { fatalError(message) }
        }
        check(NativeErrorPresentation.summary("\n \r\n  Local agent exited (code 1).\nprivate fixture log\nmore logs") == "Local agent exited (code 1).", "multiline log expanded into summary")
        let long = String(repeating: "👨‍👩‍👧‍👦", count: 300)
        let summary = NativeErrorPresentation.summary(long + "\nadditional exact details")
        check(summary.count == NativeErrorPresentation.summaryLimit + 1 && summary.hasSuffix("…"), "summary unbounded or Unicode grapheme split")
        check(long.count == 300, "display summary changed original diagnostic")
        check(NativeErrorPresentation.summary("\n \t\n") == "An error occurred.", "empty diagnostic lacks visible summary")
        check(NativeErrorPresentation.summary("Permission denied") == "Permission denied", "short error changed")
        print("Native error presentation: 5 assertions passed; display-only summaries, no error classification or GUI execution")
    }
}
