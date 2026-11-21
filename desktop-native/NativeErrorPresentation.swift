import Foundation

/// Display-only summary. The exact diagnostic remains available separately;
/// this must never be used to validate a receipt or classify an error.
enum NativeErrorPresentation {
    static let summaryLimit = 180
    static func summary(_ diagnostic: String) -> String {
        let line = diagnostic.split(whereSeparator: { $0.isNewline })
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .first(where: { !$0.isEmpty }) ?? "An error occurred."
        guard line.count > summaryLimit else { return line }
        return String(line.prefix(summaryLimit)) + "…"
    }
}
