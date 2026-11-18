import Foundation
import Network
import RelayCore
import SwiftUI

enum ConnectionState: Equatable {
    case idle, connecting, connected, reconnecting, failed

    var label: String {
        switch self {
        case .idle: return "Not connected"
        case .connecting: return "Connecting…"
        case .connected: return "Connected"
        case .reconnecting: return "Reconnecting…"
        case .failed: return "Problem"
        }
    }
}

/// All receiver behaviour: connect/disconnect, automatic reconnects, interruptions,
/// route changes and network changes. The views only display this.
@MainActor
final class ReceiverModel: ObservableObject {
    @Published private(set) var state: ConnectionState = .idle
    @Published private(set) var detail = ""
    @Published private(set) var routeName = ""
    @Published var address: String { didSet { UserDefaults.standard.set(address, forKey: Keys.address) } }
    @Published var volume: Double { didSet { session?.volume = volume; UserDefaults.standard.set(volume, forKey: Keys.volume) } }

    private enum Keys {
        static let address = "address"
        static let volume = "volume"
    }

    private let audio = AudioSessionController()
    private let pathMonitor = NWPathMonitor()
    private var session: PeerSession?
    private var desired = false          // the user wants to be connected
    private var attempt = 0
    private var retryTask: Task<Void, Never>?
    private var disconnectGrace: Task<Void, Never>?
    private var pathWasSatisfied = true

    init() {
        let d = UserDefaults.standard
        address = d.string(forKey: Keys.address) ?? ""
        volume = d.object(forKey: Keys.volume) as? Double ?? 1.0

        audio.prepare()
        routeName = audio.currentRouteName
        audio.onEvent = { [weak self] in self?.handle(audio: $0) }
        audio.onRemotePause = { [weak self] in self?.disconnect() }

        pathMonitor.pathUpdateHandler = { [weak self] path in
            Task { @MainActor in self?.handle(pathSatisfied: path.status == .satisfied) }
        }
        pathMonitor.start(queue: .global(qos: .utility))
    }

    var isActive: Bool { desired }

    // MARK: user actions

    /// `input` is the QR payload or hand-typed address; nil uses the saved address.
    func connect(to input: String? = nil) {
        if let input { address = input }
        guard RelayTarget.parse(address) != nil else {
            state = .failed
            detail = "Enter the PC's address (like 192.168.1.20), or scan the QR code."
            return
        }
        desired = true
        attempt = 0
        openSession()
    }

    func disconnect() {
        desired = false
        retryTask?.cancel()
        disconnectGrace?.cancel()
        closeSession()
        audio.deactivate()
        state = .idle
        detail = ""
    }

    /// Called when the app returns to the foreground.
    func appBecameActive() {
        routeName = audio.currentRouteName
        if desired, state != .connected { attempt = 0; openSession() }
    }

    // MARK: session management

    private func closeSession() {
        session?.onEvent = nil
        session?.close()
        session = nil
    }

    private func openSession() {
        retryTask?.cancel()
        disconnectGrace?.cancel()
        closeSession()
        guard desired, let target = RelayTarget.parse(address) else { return }

        state = attempt == 0 ? .connecting : .reconnecting
        detail = attempt == 0 ? "Contacting your PC…" : "Attempt \(attempt + 1)…"

        do { try audio.activate() } catch {
            detail = "Can't start audio right now (\(error.localizedDescription)). Another app or a call may be using it."
            scheduleReconnect(reason: detail)
            return
        }

        let s = PeerSession(target: target)
        s.volume = volume
        s.onEvent = { [weak self, weak s] event in
            Task { @MainActor in
                guard let self, let s, self.session === s else { return }
                self.handle(peer: event, host: target.host)
            }
        }
        session = s
        Task { [weak self] in
            do {
                try await s.start()
            } catch is CancellationError {
                // superseded by a newer session or a disconnect
            } catch {
                await MainActor.run {
                    guard let self, self.session === s else { return }
                    if let e = error as? SignalingError, e.isFatal {
                        self.fail(e.localizedDescription)
                    } else {
                        self.scheduleReconnect(reason: error.localizedDescription)
                    }
                }
            }
        }
    }

    private func handle(peer event: PeerEvent, host: String) {
        switch event {
        case .connected:
            disconnectGrace?.cancel()
            attempt = 0
            state = .connected
            detail = "Playing audio from \(host)."
            routeName = audio.currentRouteName
            audio.showNowPlaying(hostName: host)
        case .disconnected:
            // Usually a brief Wi-Fi hiccup: give it a moment to recover on its own.
            state = .reconnecting
            detail = "Connection interrupted…"
            disconnectGrace?.cancel()
            disconnectGrace = Task { [weak self] in
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                guard !Task.isCancelled else { return }
                self?.scheduleReconnect(reason: "Connection lost.")
            }
        case .failed(let why):
            scheduleReconnect(reason: why)
        }
    }

    private func scheduleReconnect(reason: String) {
        guard desired else { return }
        closeSession()
        retryTask?.cancel()
        attempt += 1
        let delay = RelayCore.reconnectDelay(attempt: attempt)
        state = .reconnecting
        detail = reason + (delay >= 1 ? " Retrying in \(Int(delay)) s…" : " Retrying…")
        retryTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled else { return }
            self?.openSession()
        }
    }

    private func fail(_ message: String) {
        desired = false
        retryTask?.cancel()
        closeSession()
        audio.deactivate()
        state = .failed
        detail = message
    }

    // MARK: system events

    private func handle(audio event: AudioEvent) {
        switch event {
        case .routeChanged(let name):
            routeName = name
        case .outputRemoved(let name):
            // Convention: unplugging headphones must not suddenly blast the speaker.
            guard desired else { return }
            disconnect()
            detail = "\(name) disconnected, so playback stopped. Tap Connect to play on the speaker."
        case .interruptionBegan:
            guard desired else { return }
            state = .reconnecting
            detail = "Paused by a call or another app…"
        case .interruptionEnded:
            // Restarting the connection is the most reliable way to bring playout back.
            guard desired else { return }
            attempt = 0
            openSession()
        case .mediaServicesReset:
            guard desired else { return }
            audio.prepare()
            attempt = 0
            openSession()
        }
    }

    private func handle(pathSatisfied satisfied: Bool) {
        defer { pathWasSatisfied = satisfied }
        guard desired else { return }
        if !satisfied {
            state = .reconnecting
            detail = "No network. Waiting for Wi-Fi…"
        } else if !pathWasSatisfied || state != .connected {
            attempt = 0
            openSession()   // Wi-Fi came back or changed networks
        }
    }
}
