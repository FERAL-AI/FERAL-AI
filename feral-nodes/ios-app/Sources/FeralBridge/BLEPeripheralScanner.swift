/**
 BLEPeripheralScanner — generic iOS BLE peripheral discovery
 ============================================================
 Closes thesis scenario S3: phone near peripherals → brain shows
 discovered devices → "what's near me?" answers with the list.

 The iPhone runs a `CBCentralManager` scan for nearby BLE peripherals
 (AirPods, Apple Watch, scales, heart-rate monitors, etc.), dedupes
 by stable peripheral UUID, and forwards each unique observation to
 the brain as a HUP `device_announce` frame. The brain side already
 wires this through `api/server.py::_handle_device_announce` →
 `hardware/mesh.py::ingest_device_announce` which upserts a
 knowledge-graph entity with `category=device`, so memory queries
 land via the standard tool surface.

 Background mode: the host Xcode target's `Info.plist` declares
 `bluetooth-central` under `UIBackgroundModes` to allow scanning to
 continue when the screen is locked. Apple constrains background
 scanning to peripherals the app has explicitly subscribed to via
 service-UUID filters; the foreground scan here runs with a `nil`
 service filter to discover everything.

 Throttling: each discovery announces immediately on first sight,
 then re-announces every `reannounceInterval` (default 60s) while
 the peripheral remains in range. A periodic sweep marks devices
 as "lost" via `sendDeviceLost` after `lostThreshold` (default
 120s) of silence so the brain's freshness window stays honest.

 The scanner filters out FERAL's own glasses peripheral identifiers
 (W300 / W610) via the `ownPeripheralFilter` injected at init —
 default filter matches the W300 service UUID the JW BLE adapter
 advertises. Pass `.none` to disable.

 Testing: `CBCentralManager` and `CBPeripheral` are surfaced through
 the `BLECentralManaging` and `BLEPeripheralAdvertisement` protocols
 so unit tests can drive `didDiscoverPeripheral` deterministically
 without spinning up Bluetooth hardware.
 */

import Foundation
#if canImport(CoreBluetooth)
import CoreBluetooth
#endif

// MARK: - Test seams

/// Observation passed to `BLEPeripheralScanner` on each discovery.
/// Production code wraps `CBPeripheral` + the advertisement
/// dictionary; tests construct values directly.
struct BLEPeripheralAdvertisement: Equatable {
    let identifier: UUID
    let localName: String?
    let rssi: Int
    let serviceUUIDs: [String]
    let manufacturerDataHex: String?
    let isConnectable: Bool
    let timestamp: Date

    init(
        identifier: UUID,
        localName: String? = nil,
        rssi: Int = 0,
        serviceUUIDs: [String] = [],
        manufacturerDataHex: String? = nil,
        isConnectable: Bool = true,
        timestamp: Date = Date()
    ) {
        self.identifier = identifier
        self.localName = localName
        self.rssi = rssi
        self.serviceUUIDs = serviceUUIDs
        self.manufacturerDataHex = manufacturerDataHex
        self.isConnectable = isConnectable
        self.timestamp = timestamp
    }
}

/// Decides whether a discovered peripheral is FERAL's own glasses
/// node and should be suppressed from the generic `device_announce`
/// stream. The default filter matches the JW W300 service UUID;
/// callers can extend or replace it.
struct BLEPeripheralFilter {
    let predicate: (BLEPeripheralAdvertisement) -> Bool

    init(_ predicate: @escaping (BLEPeripheralAdvertisement) -> Bool) {
        self.predicate = predicate
    }

    /// No-op filter — every observation passes through.
    static let none = BLEPeripheralFilter { _ in false }

    /// Default filter: suppress peripherals whose advertised service
    /// list contains the JW W300 / W610 service UUIDs FERAL itself
    /// emits, so the generic scanner doesn't double-announce its
    /// own glasses node.
    static let feralGlasses = BLEPeripheralFilter { ad in
        // JW W300 service UUID family (uppercase 4-byte form). Add
        // additional FERAL-owned service UUIDs here as new glasses
        // hardware lands.
        let suppressed: Set<String> = [
            "FEE0",                                  // JW W300
            "0000FEE0-0000-1000-8000-00805F9B34FB", // canonical form
        ]
        return ad.serviceUUIDs.contains { suppressed.contains($0.uppercased()) }
    }
}

/// Abstracted CoreBluetooth central. Production binding wraps
/// `CBCentralManager`; tests substitute a fake that calls back into
/// the scanner deterministically.
protocol BLECentralManaging: AnyObject {
    var bleState: BLEManagerState { get }
    func startScan()
    func stopScan()
}

enum BLEManagerState: Equatable {
    case unknown
    case resetting
    case unsupported
    case unauthorized
    case poweredOff
    case poweredOn
}

// MARK: - Settings

/// Operator-tunable throttling / lifecycle knobs. Defaults match the
/// thesis scenario timing: re-announce every minute, mark lost after
/// two minutes of silence, sweep once per 30s.
struct BLEPeripheralScannerSettings {
    var reannounceInterval: TimeInterval
    var lostThreshold: TimeInterval
    var sweepInterval: TimeInterval
    /// Maximum number of distinct peripherals tracked in the
    /// dedupe table. Beyond this the oldest entries are evicted on
    /// the next sweep so a busy environment can't unbounded-grow
    /// the cache.
    var maxTracked: Int

    init(
        reannounceInterval: TimeInterval = 60,
        lostThreshold: TimeInterval = 120,
        sweepInterval: TimeInterval = 30,
        maxTracked: Int = 256
    ) {
        self.reannounceInterval = reannounceInterval
        self.lostThreshold = lostThreshold
        self.sweepInterval = sweepInterval
        self.maxTracked = maxTracked
    }

    static let `default` = BLEPeripheralScannerSettings()
}

// MARK: - Scanner

/// Generic BLE peripheral scanner. Owned by `ConnectionManager` (or
/// equivalent app coordinator) and started after the brain client
/// connects. Thread-safe internally — all state mutations hop to a
/// dedicated serial queue.
final class BLEPeripheralScanner {
    private weak var brainClient: FeralBrainClient?
    private let settings: BLEPeripheralScannerSettings
    private let ownFilter: BLEPeripheralFilter
    private let workQueue = DispatchQueue(label: "ai.feral.ble.scanner", qos: .utility)
    private let now: () -> Date
    private var tracked: [UUID: TrackedPeripheral] = [:]
    private var central: BLECentralManaging?
    private var sweepTimer: Timer?
    private var isScanning = false

    /// Latest snapshot of every peripheral the scanner currently
    /// considers in range. Useful for SwiftUI debug panes; the brain
    /// is the source of truth for memory queries.
    var currentlyTracked: [BLEPeripheralAdvertisement] {
        workQueue.sync {
            tracked.values.map(\.lastAdvertisement)
        }
    }

    init(
        brainClient: FeralBrainClient,
        settings: BLEPeripheralScannerSettings = .default,
        ownFilter: BLEPeripheralFilter = .feralGlasses,
        now: @escaping () -> Date = Date.init
    ) {
        self.brainClient = brainClient
        self.settings = settings
        self.ownFilter = ownFilter
        self.now = now
    }

    /// Wire up the underlying CoreBluetooth central and begin
    /// scanning. Production callers leave `central` nil so the
    /// scanner constructs a real `CBCentralManager`. Tests pass a
    /// fake.
    func start(central: BLECentralManaging? = nil) {
        let manager: BLECentralManaging
        #if canImport(CoreBluetooth)
        manager = central ?? SystemBLECentral(scanner: self)
        #else
        guard let injected = central else { return }
        manager = injected
        #endif
        self.central = manager
        manager.startScan()
        startSweepTimer()
        isScanning = true
    }

    func stop() {
        central?.stopScan()
        sweepTimer?.invalidate()
        sweepTimer = nil
        isScanning = false
    }

    /// Called by the underlying central (or a test) when a new
    /// peripheral observation arrives. Filters, dedupes, and emits
    /// the HUP frame as appropriate.
    func handleDiscovery(_ ad: BLEPeripheralAdvertisement) {
        workQueue.async { [weak self] in
            guard let self = self else { return }
            guard !self.ownFilter.predicate(ad) else { return }

            let id = ad.identifier
            if var existing = self.tracked[id] {
                existing.lastAdvertisement = ad
                let elapsed = ad.timestamp.timeIntervalSince(existing.lastAnnounced)
                if elapsed >= self.settings.reannounceInterval {
                    existing.lastAnnounced = ad.timestamp
                    self.tracked[id] = existing
                    self.emitAnnounce(ad)
                } else {
                    self.tracked[id] = existing
                }
            } else {
                self.tracked[id] = TrackedPeripheral(
                    lastAdvertisement: ad,
                    firstSeen: ad.timestamp,
                    lastAnnounced: ad.timestamp
                )
                self.emitAnnounce(ad)
            }
        }
    }

    /// Manually run a freshness sweep. Production code drives this
    /// via the internal timer; tests call it directly with a fixed
    /// `now()` to assert lost-device emission.
    func runSweep() {
        workQueue.async { [weak self] in
            guard let self = self else { return }
            let now = self.now()
            var lost: [(UUID, TrackedPeripheral)] = []
            for (id, entry) in self.tracked {
                let silence = now.timeIntervalSince(entry.lastAdvertisement.timestamp)
                if silence >= self.settings.lostThreshold {
                    lost.append((id, entry))
                }
            }
            for (id, entry) in lost {
                self.tracked.removeValue(forKey: id)
                self.emitLost(entry.lastAdvertisement)
            }
            // Cap the dedupe table.
            if self.tracked.count > self.settings.maxTracked {
                let sorted = self.tracked.sorted { lhs, rhs in
                    lhs.value.lastAdvertisement.timestamp <
                        rhs.value.lastAdvertisement.timestamp
                }
                let drop = sorted.prefix(self.tracked.count - self.settings.maxTracked)
                for (id, _) in drop {
                    self.tracked.removeValue(forKey: id)
                }
            }
        }
    }

    private func emitAnnounce(_ ad: BLEPeripheralAdvertisement) {
        var manufacturer: [String: Any]? = nil
        if let hex = ad.manufacturerDataHex, !hex.isEmpty {
            manufacturer = ["raw_hex": hex]
        }
        brainClient?.sendDeviceAnnounce(
            deviceId: ad.identifier.uuidString,
            name: ad.localName ?? "",
            rssi: ad.rssi,
            services: ad.serviceUUIDs,
            manufacturerData: manufacturer,
            deviceKind: "bluetooth_le"
        )
    }

    private func emitLost(_ ad: BLEPeripheralAdvertisement) {
        brainClient?.sendDeviceLost(
            deviceId: ad.identifier.uuidString,
            name: ad.localName ?? "",
            lastRssi: ad.rssi,
            services: ad.serviceUUIDs
        )
    }

    private func startSweepTimer() {
        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }
            self.sweepTimer?.invalidate()
            self.sweepTimer = Timer.scheduledTimer(
                withTimeInterval: self.settings.sweepInterval,
                repeats: true
            ) { [weak self] _ in
                self?.runSweep()
            }
        }
    }

    private struct TrackedPeripheral {
        var lastAdvertisement: BLEPeripheralAdvertisement
        let firstSeen: Date
        var lastAnnounced: Date
    }
}

// MARK: - Production CoreBluetooth binding

#if canImport(CoreBluetooth)
/// Thin adapter wrapping `CBCentralManager`. Translates the
/// delegate callbacks into `BLEPeripheralAdvertisement` values the
/// `BLEPeripheralScanner` consumes.
final class SystemBLECentral: NSObject, BLECentralManaging, CBCentralManagerDelegate {
    private let manager: CBCentralManager
    private weak var scanner: BLEPeripheralScanner?
    private var wantsScan = false

    init(scanner: BLEPeripheralScanner) {
        self.scanner = scanner
        self.manager = CBCentralManager(delegate: nil, queue: nil)
        super.init()
        self.manager.delegate = self
    }

    var bleState: BLEManagerState {
        switch manager.state {
        case .unknown: return .unknown
        case .resetting: return .resetting
        case .unsupported: return .unsupported
        case .unauthorized: return .unauthorized
        case .poweredOff: return .poweredOff
        case .poweredOn: return .poweredOn
        @unknown default: return .unknown
        }
    }

    func startScan() {
        wantsScan = true
        if manager.state == .poweredOn {
            performScan()
        }
    }

    func stopScan() {
        wantsScan = false
        manager.stopScan()
    }

    private func performScan() {
        // CBCentralManagerScanOptionAllowDuplicatesKey=true so RSSI
        // tracking + freshness sweeps see every observation; the
        // scanner's own throttling handles re-announce cadence.
        manager.scanForPeripherals(
            withServices: nil,
            options: [CBCentralManagerScanOptionAllowDuplicatesKey: true]
        )
    }

    // MARK: CBCentralManagerDelegate

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        if central.state == .poweredOn, wantsScan {
            performScan()
        }
    }

    func centralManager(
        _ central: CBCentralManager,
        didDiscover peripheral: CBPeripheral,
        advertisementData: [String : Any],
        rssi RSSI: NSNumber
    ) {
        let services: [String] = {
            guard let raw = advertisementData[CBAdvertisementDataServiceUUIDsKey] as? [CBUUID] else {
                return []
            }
            return raw.map { $0.uuidString }
        }()
        let localName = (advertisementData[CBAdvertisementDataLocalNameKey] as? String)
            ?? peripheral.name
        let manufacturerHex: String? = {
            guard let data = advertisementData[CBAdvertisementDataManufacturerDataKey] as? Data,
                  !data.isEmpty else { return nil }
            return data.map { String(format: "%02x", $0) }.joined()
        }()
        let isConnectable = (advertisementData[CBAdvertisementDataIsConnectable] as? Bool) ?? false
        let ad = BLEPeripheralAdvertisement(
            identifier: peripheral.identifier,
            localName: localName,
            rssi: RSSI.intValue,
            serviceUUIDs: services,
            manufacturerDataHex: manufacturerHex,
            isConnectable: isConnectable,
            timestamp: Date()
        )
        scanner?.handleDiscovery(ad)
    }
}
#endif
