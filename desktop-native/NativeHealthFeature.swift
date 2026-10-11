import SwiftUI
import Foundation
import CoreFoundation

// Read-only health presentation. Source heartbeat times must never be
// presented as measurement times; the summary endpoint does not expose those.
struct NativeHealthMeasure: Identifiable {
    let id: String
    let title: String
    let unit: String
    let value: Double?
    let source: String?
    var display: String { value.map { $0.formatted(.number.precision(.fractionLength(0...2))) } ?? "Unavailable" }
}

struct NativeHealthSource: Identifiable {
    let id: String
    let name: String
    let sampleSource: String?
    let provenance: String
    let live: Bool?
    let lastSeen: Date?
    var status: String { live.map { $0 ? "Live source" : "Stale source" } ?? "Source status unavailable" }
}

enum NativeHealthParsing {
    static func number(_ value: Any?) -> Double? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
        let result = number.doubleValue
        return result.isFinite ? result : nil
    }
    static func date(_ value: Any?) -> Date? {
        guard let seconds = number(value), seconds > 0 else { return nil }
        return Date(timeIntervalSince1970: seconds)
    }
    static func measures(_ data: [String: Any]) -> [NativeHealthMeasure] {
        let fields = [
            ("current_hr", "Current heart rate", "bpm"),
            ("resting_hr", "Resting heart rate", "bpm"),
            ("current_spo2", "Current oxygen saturation", "%"),
            ("sleep_hours", "Sleep duration", "hours"),
            ("sleep_quality", "Sleep quality score", ""),
            ("hrv", "Heart rate variability", "ms"),
            ("recovery_score", "Recovery score", ""),
            ("readiness", "Readiness score", ""),
            ("activity_score", "Activity score", ""),
            ("strain", "Strain", "")
        ]
        return fields.map { key, title, unit in
            NativeHealthMeasure(id: key, title: title, unit: unit, value: number(data[key]), source: data[key + "_source"] as? String)
        }
    }
    static func sources(_ dashboard: [String: Any]) -> [NativeHealthSource] {
        let names = ["apple_healthkit": "Apple Health", "jw_health_glasses": "Theora glasses", "veepoo_wristband": "Veepoo wristband", "w610_glasses": "W610 open glasses", "generic_ble_hr": "BLE heart-rate sensor", "whoop_cloud": "Whoop", "oura_cloud": "Oura", "strava_cloud": "Strava", "garmin_cloud": "Garmin", "fitbit_cloud": "Fitbit"]
        var output: [NativeHealthSource] = []
        for (deviceIndex, device) in (dashboard["devices"] as? [[String: Any]] ?? []).enumerated() {
            for (index, row) in (device["subdevices"] as? [[String: Any]] ?? []).enumerated() {
                let capability = row["capability"] as? String ?? ""
                // The dashboard also contains microphones/cameras and other
                // peripherals. Only health pipeline capabilities belong here.
                guard names[capability] != nil else { continue }
                let attrs = row["attrs"] as? [String: Any] ?? [:]
                let node = row["node_id"] as? String ?? device["node_id"] as? String ?? "unknown"
                output.append(NativeHealthSource(id: "\(node):\(capability):\(deviceIndex):\(index)", name: names[capability] ?? (capability.isEmpty ? "Unknown source" : capability), sampleSource: attrs["device_name"] as? String ?? attrs["sample_source"] as? String, provenance: row["provenance"] as? String ?? "Unavailable", live: row["live"] as? Bool, lastSeen: date(row["last_seen"])))
            }
        }
        return output
    }
}

private struct NativeHealthFailure: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

@MainActor final class NativeHealthModel: ObservableObject {
    @Published var loading = false
    @Published var payloads: [String: [String: Any]] = [:]
    @Published var errors: [String: String] = [:]
    @Published var fetchedAt: [String: Date] = [:]
    private var baseURL: URL?
    private let session: URLSession
    private var generation = UUID()
    static let paths = ["snapshot": "/api/health-summary", "summary": "/api/baseline/summary", "metrics": "/api/baseline/metrics", "alerts": "/api/baseline/alerts", "sources": "/api/dashboard"]

    init(baseURL: URL?, session: URLSession? = nil) {
        self.baseURL = baseURL
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 20
        configuration.timeoutIntervalForResource = 30
        self.session = session ?? URLSession(configuration: configuration)
    }
    func setBaseURL(_ url: URL?) {
        guard url != baseURL else { return }
        generation = UUID(); baseURL = url
        payloads = [:]; errors = [:]; fetchedAt = [:]; loading = false
    }
    func refresh() async {
        guard !loading else { return }
        guard let baseURL else {
            errors = Dictionary(uniqueKeysWithValues: Self.paths.keys.map { ($0, "The local agent is not ready yet.") })
            return
        }
        guard ["127.0.0.1", "::1", "[::1]"].contains(baseURL.host ?? ""), ["http", "https"].contains(baseURL.scheme ?? ""), baseURL.user == nil, baseURL.password == nil else {
            errors = Dictionary(uniqueKeysWithValues: Self.paths.keys.map { ($0, "Health data is available only from the local agent's loopback address.") })
            return
        }
        loading = true
        let current = generation
        defer { if generation == current { loading = false } }
        for key in Self.paths.keys.sorted() {
            guard generation == current, !Task.isCancelled else { return }
            do {
                let url = URL(string: Self.paths[key]!, relativeTo: baseURL)!
                let (data, response) = try await session.data(from: url)
                guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else { throw NativeHealthFailure(message: "Health data request failed. Retry to refresh this section.") }
                guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw NativeHealthFailure(message: "The local agent returned an unreadable health response.") }
                if let error = object["error"] as? String, !error.isEmpty { throw NativeHealthFailure(message: error) }
                if key == "snapshot", !(object["data"] is [String: Any]) { throw NativeHealthFailure(message: "Health snapshot data is unavailable.") }
                if (key == "metrics" && !(object["metrics"] is [[String: Any]])) || (key == "alerts" && !(object["alerts"] is [[String: Any]])) || (key == "sources" && !(object["devices"] is [[String: Any]])) {
                    throw NativeHealthFailure(message: "This health section returned an unreadable data list.")
                }
                guard generation == current, !Task.isCancelled else { return }
                payloads[key] = object; errors.removeValue(forKey: key); fetchedAt[key] = Date()
            } catch {
                guard generation == current, !Task.isCancelled else { return }
                errors[key] = error.localizedDescription
            }
        }
    }
    var measures: [NativeHealthMeasure] { NativeHealthParsing.measures(payloads["snapshot"]?["data"] as? [String: Any] ?? [:]) }
    var sources: [NativeHealthSource] { NativeHealthParsing.sources(payloads["sources"] ?? [:]) }
}

struct NativeHealthFeatureView: View {
    let baseURL: URL?
    @StateObject private var model: NativeHealthModel
    @State private var tab = "overview"

    init(baseURL: URL?) {
        self.baseURL = baseURL
        _model = StateObject(wrappedValue: NativeHealthModel(baseURL: baseURL))
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Label("Health", systemImage: "heart.text.square").font(.largeTitle.bold())
                Spacer()
                if model.loading { ProgressView().controlSize(.small) }
                Button("Refresh") { Task { await model.refresh() } }.disabled(model.loading || baseURL == nil)
            }
            Text("Your recorded measurements and learned baselines. Informational observations; these do not establish a diagnosis.").foregroundStyle(.secondary)
            Picker("Health section", selection: $tab) {
                Text("Overview").tag("overview"); Text("Baselines").tag("baselines"); Text("Alerts").tag("alerts"); Text("Sources").tag("sources")
            }.pickerStyle(.segmented)
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if tab == "overview" { overview }
                    else if tab == "baselines" { baselines }
                    else if tab == "alerts" { alerts }
                    else { sources }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
        }.padding(24)
        .task(id: baseURL) { model.setBaseURL(baseURL); await model.refresh() }
    }
    @ViewBuilder private func status(_ key: String) -> some View {
        if let error = model.errors[key] {
            Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
            if model.payloads[key] != nil { Text("Showing the previously fetched data; refresh failed.").font(.caption).foregroundStyle(.secondary) }
        } else if model.payloads[key] == nil {
            Text(model.loading ? "Loading…" : "Data unavailable").foregroundStyle(.secondary)
        }
        if let date = model.fetchedAt[key] { Text("Fetched \(date.formatted(date: .abbreviated, time: .standard))").font(.caption).foregroundStyle(.secondary) }
    }
    private var overview: some View {
        VStack(alignment: .leading, spacing: 16) {
            status("snapshot")
            if model.payloads["snapshot"] != nil {
                Text("The snapshot API does not include measurement timestamps. Fetch time is when this view refreshed; individual sample freshness is unavailable.").font(.caption).foregroundStyle(.secondary)
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 220))], alignment: .leading, spacing: 12) {
                    ForEach(model.measures) { measurement in
                        GroupBox {
                            VStack(alignment: .leading, spacing: 6) {
                                Text(measurement.title).font(.headline)
                                Text(measurement.display + (measurement.value == nil || measurement.unit.isEmpty ? "" : " " + measurement.unit)).font(.title2)
                                Text("Source: " + (measurement.source ?? "Not provided per measurement")).font(.caption).foregroundStyle(.secondary)
                                Text("Measurement time: unavailable").font(.caption).foregroundStyle(.secondary)
                            }.frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
                if let names = model.payloads["snapshot"]?["data"] as? [String: Any], let sources = names["sources"] as? [String], !sources.isEmpty {
                    Text("Snapshot source list: " + sources.joined(separator: ", ")).font(.caption)
                }
            }
        }
    }
    private var baselines: some View {
        VStack(alignment: .leading, spacing: 12) {
            status("summary")
            if let summary = model.payloads["summary"] {
                HStack {
                    Text("Metrics tracked: " + count(summary["metrics_tracked"]))
                    Text("Alerts in the last 24 hours: " + count(summary["recent_alerts"]))
                }
                if let categories = summary["categories"] as? [String] { Text("Categories: " + (categories.isEmpty ? "None recorded" : categories.joined(separator: ", "))).foregroundStyle(.secondary) }
            }
            Divider(); status("metrics")
            if let metrics = model.payloads["metrics"]?["metrics"] as? [[String: Any]] {
                if metrics.isEmpty { Text(model.errors["summary"] == nil ? "No baseline metrics recorded." : "No metric rows returned; baseline engine availability could not be verified.").foregroundStyle(.secondary) }
                ForEach(Array(metrics.enumerated()), id: \.offset) { _, metric in
                    GroupBox {
                        VStack(alignment: .leading, spacing: 6) {
                            Text(metric["metric_id"] as? String ?? "Unnamed metric").font(.headline)
                            Text("Mean: " + number(metric["mean"]) + " · Standard deviation: " + number(metric["std_dev"]))
                            Text("Samples: " + ((metric["values"] as? [Any]).map { String($0.count) } ?? "Unavailable"))
                            Text("Category: " + (metric["category"] as? String ?? "Unavailable"))
                            Text("Updated: " + timestamp(metric["last_updated"]))
                            Text("Sample units and provenance are not supplied by this endpoint.").font(.caption).foregroundStyle(.secondary)
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
    }
    private var alerts: some View {
        VStack(alignment: .leading, spacing: 12) {
            status("alerts")
            Text("Alerts reflect the baseline engine's observed deviations; absence of a recorded alert does not establish health status.").font(.caption).foregroundStyle(.secondary)
            if let alerts = model.payloads["alerts"]?["alerts"] as? [[String: Any]] {
                if alerts.isEmpty { Text(model.errors["summary"] == nil ? "No recorded baseline alerts." : "No alert rows returned; baseline engine availability could not be verified.").foregroundStyle(.secondary) }
                ForEach(Array(alerts.enumerated()), id: \.offset) { _, alert in
                    GroupBox {
                        VStack(alignment: .leading, spacing: 6) {
                            Text(alert["metric_id"] as? String ?? "Unnamed metric").font(.headline)
                            Text("Severity: " + (alert["severity"] as? String ?? "Unavailable") + " · " + (alert["alert_type"] as? String ?? ""))
                            Text(alert["message"] as? String ?? "Alert description unavailable")
                            Text("Recorded: " + timestamp(alert["timestamp"])).font(.caption).foregroundStyle(.secondary)
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
    }
    private var sources: some View {
        VStack(alignment: .leading, spacing: 12) {
            status("sources")
            Text("Live/stale describes the source heartbeat, not the age of a particular health measurement.").font(.caption).foregroundStyle(.secondary)
            if let unavailable = model.payloads["sources"]?["subdevices_unavailable"] as? String { Label("Source registry unavailable: " + unavailable, systemImage: "exclamationmark.triangle").foregroundStyle(.orange) }
            if model.payloads["sources"] != nil && model.sources.isEmpty { Text("No sources returned in the paired-device registry.").foregroundStyle(.secondary) }
            ForEach(model.sources) { source in
                GroupBox {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(source.name).font(.headline)
                        Text(source.status).foregroundStyle(source.live == true ? Color.primary : Color.secondary)
                        if let sample = source.sampleSource, !sample.isEmpty { Text("Sample source: " + sample) }
                        Text("Provenance: " + source.provenance)
                        Text("Last heartbeat: " + (source.lastSeen.map { $0.formatted(date: .abbreviated, time: .standard) } ?? "Unavailable")).font(.caption).foregroundStyle(.secondary)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }
    private func number(_ value: Any?) -> String { NativeHealthParsing.number(value).map { $0.formatted(.number.precision(.fractionLength(0...2))) } ?? "Unavailable" }
    private func count(_ value: Any?) -> String { number(value) }
    private func timestamp(_ value: Any?) -> String { NativeHealthParsing.date(value).map { $0.formatted(date: .abbreviated, time: .standard) } ?? "Unavailable" }
}
