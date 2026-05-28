import Foundation
import Combine

class ConnectionManager: ObservableObject {
    @Published var isConnected = false
    @Published var brainHost = ""
    @Published var brainPort = 9090
    @Published var apiKey = ""
    @Published var nodeName = "iPhone"
    @Published var lastHeartRate: Int = 0
    @Published var lastSpO2: Int = 0
    @Published var statusMessage = "Not connected"

    private(set) var client: FeralBrainClient?
    /// Generic BLE peripheral scanner. Started after the brain
    /// connection is up so every `device_announce` frame carries a
    /// valid scanner node id; stopped on disconnect.
    private(set) var bleScanner: BLEPeripheralScanner?
    /// Phone-camera-as-glasses adapter. Lazily started when the
    /// brain confirms registration; emits one `glasses_frame` per
    /// `CameraGlassesAdapter.Settings.framePeriod` while running.
    private(set) var cameraGlasses: CameraGlassesAdapter?

    func connect() {
        guard !brainHost.isEmpty else { return }
        let newClient = FeralBrainClient(
            host: brainHost,
            port: brainPort,
            nodeId: nodeName,
            useTLS: brainPort == 9443
        )
        client = newClient
        newClient.connect(apiKey: apiKey)
        statusMessage = "Connecting..."
        bleScanner = BLEPeripheralScanner(brainClient: newClient)
        bleScanner?.start()
        cameraGlasses = CameraGlassesAdapter(brainClient: newClient)
        cameraGlasses?.start()
    }

    func disconnect() {
        bleScanner?.stop()
        bleScanner = nil
        cameraGlasses?.stop()
        cameraGlasses = nil
        client?.disconnect()
        client = nil
        isConnected = false
        statusMessage = "Disconnected"
    }

    func configureFromPairing(_ info: PairingInfo) {
        brainHost = info.host
        brainPort = info.port
        apiKey = info.apiKey
        nodeName = info.nodeName
        connect()
    }

    func sendText(_ text: String) {
        client?.sendTextCommand(text)
    }
}
