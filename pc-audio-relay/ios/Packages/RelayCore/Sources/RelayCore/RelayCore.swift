import Foundation

/// Where the Windows sender lives. Parsed from the QR code or typed by hand.
public struct RelayTarget: Equatable, Sendable {
    public static let defaultPort = 8765

    public var host: String
    public var port: Int
    public var key: String?

    public init(host: String, port: Int = RelayTarget.defaultPort, key: String? = nil) {
        self.host = host
        self.port = port
        self.key = key
    }

    /// `http://host:port` (IPv6 literals get brackets).
    public var baseURL: URL {
        let h = host.contains(":") ? "[\(host)]" : host
        return URL(string: "http://\(h):\(port)")!
    }

    public var displayAddress: String { "\(host):\(port)" }

    /// Accepts the QR payload (`http://192.168.1.20:8765/?k=abc123`) as well as
    /// hand-typed forms: `192.168.1.20`, `192.168.1.20:8765`, `my-pc.local`.
    public static func parse(_ input: String) -> RelayTarget? {
        var text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        // Newer Foundation versions percent-encode spaces instead of failing, so reject them here.
        guard !text.isEmpty, !text.contains(where: { $0.isWhitespace }) else { return nil }
        if !text.contains("://") { text = "http://" + text }
        guard let comps = URLComponents(string: text),
              comps.scheme?.lowercased() == "http",
              let host = comps.host, !host.isEmpty else { return nil }
        let port = comps.port ?? defaultPort
        guard (1...65535).contains(port) else { return nil }
        let key = comps.queryItems?.first(where: { $0.name == "k" })?.value
        return RelayTarget(host: host, port: port, key: (key?.isEmpty ?? true) ? nil : key)
    }
}

public enum RelayCore {
    /// Delay before reconnect attempt number `attempt` (1-based): a quick first
    /// retry, then 1, 2, 4, 8 s. Same schedule as the Safari receiver.
    public static func reconnectDelay(attempt: Int) -> TimeInterval {
        if attempt <= 1 { return 0.25 }
        return min(8, pow(2, Double(attempt - 2)))
    }

    /// Opus is always "opus/48000/2", but receivers only decode stereo when the
    /// SDP says `stereo=1`. Adds `stereo=1;sprop-stereo=1` to the Opus fmtp line,
    /// creating the line if needed. Idempotent.
    public static func addOpusStereo(_ sdp: String) -> String {
        var lines = sdp.replacingOccurrences(of: "\r\n", with: "\n")
            .split(separator: "\n", omittingEmptySubsequences: true).map(String.init)

        var payloadTypes: [String] = []
        for line in lines {
            let lower = line.lowercased()
            guard lower.hasPrefix("a=rtpmap:"), lower.contains(" opus/48000/2") else { continue }
            if let pt = line.dropFirst("a=rtpmap:".count).split(separator: " ").first {
                payloadTypes.append(String(pt))
            }
        }

        for pt in payloadTypes {
            let prefix = "a=fmtp:\(pt) "
            var found = false
            for i in lines.indices where lines[i].hasPrefix(prefix) {
                found = true
                var params = String(lines[i].dropFirst(prefix.count))
                for (key, setting) in [("stereo", "stereo=1"), ("sprop-stereo", "sprop-stereo=1")] {
                    let present = params.split(separator: ";").contains {
                        $0.trimmingCharacters(in: .whitespaces).hasPrefix("\(key)=")
                    }
                    if !present { params += ";" + setting }
                }
                lines[i] = prefix + params
            }
            if !found, let idx = lines.firstIndex(where: { $0.lowercased().hasPrefix("a=rtpmap:\(pt) opus") }) {
                lines.insert("a=fmtp:\(pt) minptime=10;stereo=1;sprop-stereo=1", at: idx + 1)
            }
        }
        return lines.joined(separator: "\r\n") + "\r\n"
    }
}
