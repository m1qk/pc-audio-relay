import Foundation
import RelayCore
#if canImport(LiveKitWebRTC)
import LiveKitWebRTC
#elseif canImport(WebRTC)
import WebRTC
#endif

enum PeerEvent {
    case connected
    case disconnected          // may recover on its own within a few seconds
    case failed(String)        // give up on this session and reconnect
}

enum PeerError: LocalizedError {
    case noSDP
    case connectTimeout(String)
    var errorDescription: String? {
        switch self {
        case .noSDP: return "Could not create the audio connection."
        case .connectTimeout(let why): return why
        }
    }
}

/// One WebRTC connection to the sender: receive-only stereo Opus, LAN only (no ICE servers).
final class PeerSession: NSObject, RTCPeerConnectionDelegate {
    private static let factory: RTCPeerConnectionFactory = RTCPeerConnectionFactory()

    private let target: RelayTarget
    private let signaling: SignalingClient
    private var pc: RTCPeerConnection?
    private var remoteTrack: RTCAudioTrack?
    private var sessionId: String?
    private var watchdog: Task<Void, Never>?
    private var closed = false

    var onEvent: ((PeerEvent) -> Void)?
    /// 0...1. Applied to the remote track (the iPhone's hardware buttons control the rest).
    var volume: Double = 1.0 { didSet { remoteTrack?.source.volume = volume } }

    init(target: RelayTarget) {
        self.target = target
        self.signaling = SignalingClient(target: target)
    }

    // MARK: lifecycle

    func start() async throws {
        let config = RTCConfiguration()
        config.iceServers = []                       // local network only
        config.sdpSemantics = .unifiedPlan
        config.continualGatheringPolicy = .gatherOnce
        let constraints = RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil)
        guard let pc = Self.factory.peerConnection(with: config, constraints: constraints, delegate: self) else {
            throw PeerError.noSDP
        }
        self.pc = pc

        let tInit = RTCRtpTransceiverInit()
        tInit.direction = .recvOnly
        pc.addTransceiver(of: .audio, init: tInit)

        let offer: RTCSessionDescription = try await withCheckedThrowingContinuation { cont in
            pc.offer(for: RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil)) { sdp, error in
                if let sdp { cont.resume(returning: sdp) } else { cont.resume(throwing: error ?? PeerError.noSDP) }
            }
        }
        let stereoOffer = RTCSessionDescription(type: .offer, sdp: RelayCore.addOpusStereo(offer.sdp))
        try await pc.setLocalDescription(stereoOffer)

        // Include the host candidates in the offer (up to 1.5 s).
        for _ in 0..<30 where pc.iceGatheringState != .complete {
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        guard !closed, let localSDP = pc.localDescription?.sdp else { throw CancellationError() }

        let answer = try await signaling.sendOffer(localSDP)
        sessionId = answer.sessionId
        guard !closed else { hangup(); throw CancellationError() }
        try await pc.setRemoteDescription(RTCSessionDescription(type: .answer, sdp: answer.sdp))
        startWatchdog()
    }

    func close() {
        closed = true
        watchdog?.cancel()
        hangup()
        remoteTrack = nil
        pc?.delegate = nil
        pc?.close()
        pc = nil
    }

    private func hangup() {
        if let sessionId { signaling.hangup(sessionId: sessionId) }
        sessionId = nil
    }

    // MARK: health

    /// Fails the session if it never connects, or if packets stop arriving while it claims to be connected.
    private func startWatchdog() {
        watchdog?.cancel()
        let started = Date()
        watchdog = Task { [weak self] in
            var lastPackets = -1
            var lastChange = Date()
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                guard let self, let pc = self.pc, !self.closed else { return }
                if pc.connectionState != .connected {
                    // Never got connected: ask the PC why (it knows when UDP is blocked) and give up on this session.
                    let stillConnecting = pc.connectionState == .new || pc.connectionState == .connecting
                    if stillConnecting, Date().timeIntervalSince(started) > 10 {
                        let why = await self.sessionId.asyncFlatMap { await self.signaling.failureReason(sessionId: $0) }
                            ?? "The PC answered, but the audio connection did not complete. Check Windows Firewall and that both devices are on the same network."
                        self.onEvent?(.failed(why))
                        return
                    }
                    continue
                }
                let packets = await self.inboundPackets(pc)
                if packets != lastPackets { lastPackets = packets; lastChange = Date() }
                else if Date().timeIntervalSince(lastChange) > 2.5 {
                    self.onEvent?(.failed("Audio stopped arriving."))
                    return
                }
            }
        }
    }

    private func inboundPackets(_ pc: RTCPeerConnection) async -> Int {
        await withCheckedContinuation { cont in
            pc.statistics { report in
                var count = -1
                for (_, stat) in report.statistics where stat.type == "inbound-rtp" {
                    if let n = stat.values["packetsReceived"] as? NSNumber { count = n.intValue }
                }
                cont.resume(returning: count)
            }
        }
    }

    // MARK: RTCPeerConnectionDelegate

    func peerConnection(_ peerConnection: RTCPeerConnection, didAdd rtpReceiver: RTCRtpReceiver, streams: [RTCMediaStream]) {
        guard let track = rtpReceiver.track as? RTCAudioTrack else { return }
        track.isEnabled = true
        track.source.volume = volume
        remoteTrack = track  // keep a strong reference or playout stops
    }

    func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCPeerConnectionState) {
        switch newState {
        case .connected: onEvent?(.connected)
        case .disconnected: onEvent?(.disconnected)
        case .failed: onEvent?(.failed("The audio connection failed."))
        default: break
        }
    }

    func peerConnection(_ peerConnection: RTCPeerConnection, didChange stateChanged: RTCSignalingState) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didAdd stream: RTCMediaStream) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didRemove stream: RTCMediaStream) {}
    func peerConnectionShouldNegotiate(_ peerConnection: RTCPeerConnection) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceConnectionState) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceGatheringState) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didGenerate candidate: RTCIceCandidate) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didRemove candidates: [RTCIceCandidate]) {}
    func peerConnection(_ peerConnection: RTCPeerConnection, didOpen dataChannel: RTCDataChannel) {}
}

private extension Optional {
    func asyncFlatMap<T>(_ transform: (Wrapped) async -> T?) async -> T? {
        guard let self else { return nil }
        return await transform(self)
    }
}
