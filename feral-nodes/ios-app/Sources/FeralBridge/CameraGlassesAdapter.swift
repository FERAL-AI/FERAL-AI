/**
 CameraGlassesAdapter — phone camera as the canonical glasses node
 ==================================================================
 Closes thesis scenario S5's last-mile gap: without W610 hardware
 we still need a glasses-class vision stream for the brain's
 `GlassesBuffer` to consume. This adapter wraps `AVCaptureSession`
 on the iPhone back camera, encodes each captured frame to JPEG at
 ~1 fps (configurable), and emits a HUP `glasses_frame` envelope
 via `FeralBrainClient.sendGlassesFrame(...)`.

 The brain treats the resulting stream identically to a real W610
 feed: `state.glasses_buffer` ingests by `device_id` and the
 orchestrator's vision-context-attach reads it on voice / chat
 turns. The `source: "camera_fallback"` field labels provenance so
 the orchestrator can choose a cheaper vision model tier when the
 cost router is wired to discriminate sources.

 Lifecycle:
   start()  — request `.video` permission, configure 720p back
              camera + JPEG output, fire frames on the work queue
              every `settings.framePeriod` seconds.
   stop()   — tear down the session and stop emitting.

 The session intentionally runs at a low frame rate (1 fps default)
 to stay under the brain's 512 KiB-per-frame cap (HUP §2) and to
 avoid burning bandwidth on a phone tether. Increase
 `settings.framePeriod` to fewer fps for slower links.

 Without `AVFoundation` (e.g. running in `swift test` on Linux) the
 adapter compiles as a stub so the package still builds; `start()`
 reports the situation via the optional `onUnavailable` callback
 and otherwise no-ops.

 QCSDK / W610 status: see `feral-nodes/ios-node-sdk/Sources/
 FeralNodeSDK/Adapters/QCSDKAdapter.swift` — the vendor adapter
 still throws `adapterNotWired` because the W610 hardware /
 framework drop is not in this checkout. Until that lands the
 phone camera *is* the canonical glasses node. Document this in
 the operator's Settings copy so they know which path is live.
 */

import Foundation
#if canImport(AVFoundation)
import AVFoundation
#endif
#if canImport(CoreImage)
import CoreImage
#endif
#if canImport(UIKit)
import UIKit
#endif

// MARK: - Settings

struct CameraGlassesAdapterSettings {
    /// Seconds between emitted frames. Default 1.0 (≈1 fps).
    var framePeriod: TimeInterval
    /// JPEG compression quality (0...1). 0.6 keeps a 1280×720 frame
    /// comfortably under the brain's 512 KiB-per-frame cap.
    var jpegQuality: CGFloat
    /// Provenance label forwarded into `glasses_frame.source`. The
    /// brain treats `camera_fallback` as a cheaper-tier hint.
    var source: String
    /// Long-form, operator-facing copy explaining why the phone
    /// camera is serving as the glasses node. Shown by Settings'
    /// "Glasses status" row.
    var operatorNote: String

    init(
        framePeriod: TimeInterval = 1.0,
        jpegQuality: CGFloat = 0.6,
        source: String = "camera_fallback",
        operatorNote: String = CameraGlassesAdapterSettings.defaultOperatorNote
    ) {
        self.framePeriod = framePeriod
        self.jpegQuality = jpegQuality
        self.source = source
        self.operatorNote = operatorNote
    }

    static let `default` = CameraGlassesAdapterSettings()

    static let defaultOperatorNote =
        "QCSDK glasses adapter requires W610 hardware; using phone " +
        "camera node instead. Each captured frame is sent to the " +
        "brain as a HUP glasses_frame at ~1 fps for vision-context " +
        "attach on voice and chat turns."
}

// MARK: - Test seam

/// Lightweight observation that the adapter emits per frame. Tests
/// inject a `FrameSource` that yields these directly without
/// spinning up `AVCaptureSession`.
struct CameraGlassesFrame {
    let jpegData: Data
    let width: Int
    let height: Int
    let timestamp: Date

    init(jpegData: Data, width: Int, height: Int, timestamp: Date = Date()) {
        self.jpegData = jpegData
        self.width = width
        self.height = height
        self.timestamp = timestamp
    }
}

/// Strategy that yields frames into a callback. Production binding
/// drives this from `AVCaptureSession`; tests post frames manually.
protocol CameraGlassesFrameSource: AnyObject {
    /// Begin producing frames. The adapter installs a handler that
    /// forwards each frame into `emit(frame:)`.
    func start(handler: @escaping (CameraGlassesFrame) -> Void) throws
    func stop()
}

/// Reasons the adapter declined to start. Surfaced to the host app
/// via `onUnavailable` so the operator sees the same copy in
/// Settings and in any debug log.
enum CameraGlassesUnavailableReason: Error, Equatable {
    case permissionDenied
    case noCamera
    case avFoundationMissing
    case sessionConfigurationFailed(String)
}

// MARK: - Adapter

final class CameraGlassesAdapter {
    private weak var brainClient: FeralBrainClient?
    private let settings: CameraGlassesAdapterSettings
    private let frameSource: CameraGlassesFrameSource?
    private var running = false
    private var sequence: Int = 0
    private let workQueue = DispatchQueue(label: "ai.feral.glasses.camera", qos: .userInitiated)
    var onUnavailable: ((CameraGlassesUnavailableReason) -> Void)?

    /// Most recent JPEG byte count actually emitted. Useful for a
    /// SwiftUI debug pane; not authoritative.
    private(set) var lastFrameBytes: Int = 0

    init(
        brainClient: FeralBrainClient,
        settings: CameraGlassesAdapterSettings = .default,
        frameSource: CameraGlassesFrameSource? = nil
    ) {
        self.brainClient = brainClient
        self.settings = settings
        self.frameSource = frameSource
    }

    /// Start emitting `glasses_frame` envelopes. Production callers
    /// leave `frameSource` nil so the adapter constructs the
    /// `AVCaptureSession`-backed source on iOS. Tests inject a
    /// deterministic source.
    func start() {
        guard !running else { return }
        let source: CameraGlassesFrameSource
        if let injected = frameSource {
            source = injected
        } else {
            #if canImport(AVFoundation) && canImport(UIKit)
            source = SystemCameraFrameSource(settings: settings)
            #else
            onUnavailable?(.avFoundationMissing)
            return
            #endif
        }
        do {
            try source.start { [weak self] frame in
                self?.handle(frame: frame, source: source)
            }
            running = true
        } catch let reason as CameraGlassesUnavailableReason {
            onUnavailable?(reason)
        } catch {
            onUnavailable?(.sessionConfigurationFailed(error.localizedDescription))
        }
    }

    func stop() {
        guard running else { return }
        running = false
        frameSource?.stop()
    }

    /// Test seam: feed a frame directly without going through the
    /// `AVCaptureSession` pipeline.
    func ingest(frame: CameraGlassesFrame) {
        handle(frame: frame, source: nil)
    }

    private func handle(frame: CameraGlassesFrame, source _: CameraGlassesFrameSource?) {
        workQueue.async { [weak self] in
            guard let self = self, let brain = self.brainClient else { return }
            self.sequence += 1
            self.lastFrameBytes = frame.jpegData.count
            let b64 = frame.jpegData.base64EncodedString()
            brain.sendGlassesFrame(
                deviceId: brain.nodeId,
                jpegBase64: b64,
                width: frame.width,
                height: frame.height,
                source: self.settings.source,
                sequence: self.sequence
            )
        }
    }
}

// MARK: - Production AVCaptureSession binding

#if canImport(AVFoundation) && canImport(UIKit)
final class SystemCameraFrameSource: NSObject, CameraGlassesFrameSource,
                                       AVCaptureVideoDataOutputSampleBufferDelegate {
    private let settings: CameraGlassesAdapterSettings
    private let session = AVCaptureSession()
    private let sessionQueue = DispatchQueue(label: "ai.feral.glasses.session")
    private let outputQueue = DispatchQueue(label: "ai.feral.glasses.output")
    private var handler: ((CameraGlassesFrame) -> Void)?
    private var lastEmit = Date.distantPast
    private let ciContext = CIContext(options: nil)

    init(settings: CameraGlassesAdapterSettings) {
        self.settings = settings
        super.init()
    }

    func start(handler: @escaping (CameraGlassesFrame) -> Void) throws {
        self.handler = handler
        let status = AVCaptureDevice.authorizationStatus(for: .video)
        switch status {
        case .authorized:
            try configureAndStart()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
                guard let self = self else { return }
                if granted {
                    self.sessionQueue.async {
                        try? self.configureAndStart()
                    }
                } else {
                    self.handler = nil
                }
            }
        default:
            throw CameraGlassesUnavailableReason.permissionDenied
        }
    }

    func stop() {
        sessionQueue.async { [weak self] in
            self?.session.stopRunning()
        }
        handler = nil
    }

    private func configureAndStart() throws {
        guard let camera = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) else {
            throw CameraGlassesUnavailableReason.noCamera
        }
        let input: AVCaptureDeviceInput
        do {
            input = try AVCaptureDeviceInput(device: camera)
        } catch {
            throw CameraGlassesUnavailableReason.sessionConfigurationFailed(error.localizedDescription)
        }
        session.beginConfiguration()
        if session.canSetSessionPreset(.hd1280x720) {
            session.sessionPreset = .hd1280x720
        }
        if session.canAddInput(input) {
            session.addInput(input)
        } else {
            session.commitConfiguration()
            throw CameraGlassesUnavailableReason.sessionConfigurationFailed("addInput rejected")
        }
        let output = AVCaptureVideoDataOutput()
        output.alwaysDiscardsLateVideoFrames = true
        output.videoSettings = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
        ]
        output.setSampleBufferDelegate(self, queue: outputQueue)
        if session.canAddOutput(output) {
            session.addOutput(output)
        } else {
            session.commitConfiguration()
            throw CameraGlassesUnavailableReason.sessionConfigurationFailed("addOutput rejected")
        }
        session.commitConfiguration()
        sessionQueue.async { [weak self] in
            self?.session.startRunning()
        }
    }

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        let now = Date()
        if now.timeIntervalSince(lastEmit) < settings.framePeriod { return }
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let ci = CIImage(cvPixelBuffer: pixelBuffer)
        let width = Int(ci.extent.width)
        let height = Int(ci.extent.height)
        guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
              let jpeg = ciContext.jpegRepresentation(
                of: ci,
                colorSpace: colorSpace,
                options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: settings.jpegQuality]
              )
        else { return }
        lastEmit = now
        handler?(CameraGlassesFrame(jpegData: jpeg, width: width, height: height, timestamp: now))
    }
}
#endif
