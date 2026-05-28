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
    
    func connect() {
        guard !brainHost.isEmpty else { return }
        client = FeralBrainClient(
            host: brainHost,
            port: brainPort,
            nodeId: nodeName,
            useTLS: brainPort == 9443
        )
        client?.connect(apiKey: apiKey)
        statusMessage = "Connecting..."
    }
    
    func disconnect() {
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
