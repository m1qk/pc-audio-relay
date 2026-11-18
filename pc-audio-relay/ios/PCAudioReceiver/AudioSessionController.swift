import AVFoundation
import Foundation
import MediaPlayer
#if canImport(LiveKitWebRTC)
import LiveKitWebRTC
#elseif canImport(WebRTC)
import WebRTC
#endif

enum AudioEvent {
    case interruptionBegan
    case interruptionEnded(shouldResume: Bool)
    case routeChanged(name: String)
    case outputRemoved(name: String)   // headphones unplugged / Bluetooth disconnected
    case mediaServicesReset
}

/// Owns the iOS audio session: playback category (so audio continues with the
/// screen locked, together with the `audio` background mode in Info.plist),
/// interruptions, route changes and the lock-screen controls.
@MainActor
final class AudioSessionController {
    var onEvent: ((AudioEvent) -> Void)?
    var onRemotePause: (() -> Void)?

    private var observers: [NSObjectProtocol] = []

    var currentRouteName: String {
        let outs = AVAudioSession.sharedInstance().currentRoute.outputs
        return outs.isEmpty ? "No output" : outs.map(\.portName).joined(separator: ", ")
    }

    /// Call once at launch, BEFORE the first peer connection is created.
    func prepare() {
        // libwebrtc defaults to a voice-chat configuration (playAndRecord + microphone).
        // This app only plays audio, so tell it to use plain playback instead.
        let config = RTCAudioSessionConfiguration.webRTC()
        config.category = AVAudioSession.Category.playback.rawValue
        config.mode = AVAudioSession.Mode.default.rawValue
        config.categoryOptions = []
        RTCAudioSessionConfiguration.setWebRTC(config)

        let nc = NotificationCenter.default
        let session = AVAudioSession.sharedInstance()
        observers.append(nc.addObserver(forName: AVAudioSession.interruptionNotification, object: session, queue: .main) { [weak self] note in
            MainActor.assumeIsolated { self?.handleInterruption(note) }
        })
        observers.append(nc.addObserver(forName: AVAudioSession.routeChangeNotification, object: session, queue: .main) { [weak self] note in
            MainActor.assumeIsolated { self?.handleRouteChange(note) }
        })
        observers.append(nc.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: session, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.onEvent?(.mediaServicesReset) }
        })
    }

    /// Takes the audio session. Throws if another app holds it (e.g. a phone call).
    func activate() throws {
        let s = AVAudioSession.sharedInstance()
        try s.setCategory(.playback, mode: .default, options: [])
        try s.setPreferredIOBufferDuration(0.01)   // 10 ms hardware buffer: small, but safe on all current iPhones
        try s.setActive(true)
    }

    func deactivate() {
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        clearNowPlaying()
    }

    // MARK: lock screen

    func showNowPlaying(hostName: String) {
        MPNowPlayingInfoCenter.default().nowPlayingInfo = [
            MPMediaItemPropertyTitle: "PC audio",
            MPMediaItemPropertyArtist: hostName,
            MPNowPlayingInfoPropertyIsLiveStream: true,
        ]
        let cc = MPRemoteCommandCenter.shared()
        cc.pauseCommand.removeTarget(nil)
        cc.pauseCommand.isEnabled = true
        cc.pauseCommand.addTarget { [weak self] _ in
            MainActor.assumeIsolated { self?.onRemotePause?() }
            return .success
        }
        cc.playCommand.isEnabled = false
    }

    func clearNowPlaying() {
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
        MPRemoteCommandCenter.shared().pauseCommand.removeTarget(nil)
    }

    // MARK: notifications

    private func handleInterruption(_ note: Notification) {
        guard let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
        switch type {
        case .began:
            onEvent?(.interruptionBegan)
        case .ended:
            let optRaw = note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
            let resume = AVAudioSession.InterruptionOptions(rawValue: optRaw).contains(.shouldResume)
            onEvent?(.interruptionEnded(shouldResume: resume))
        @unknown default:
            break
        }
    }

    private func handleRouteChange(_ note: Notification) {
        guard let raw = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt,
              let reason = AVAudioSession.RouteChangeReason(rawValue: raw) else { return }
        if reason == .oldDeviceUnavailable,
           let prev = note.userInfo?[AVAudioSessionRouteChangePreviousRouteKey] as? AVAudioSessionRouteDescription,
           let name = prev.outputs.first?.portName {
            onEvent?(.outputRemoved(name: name))
        }
        onEvent?(.routeChanged(name: currentRouteName))
    }
}
