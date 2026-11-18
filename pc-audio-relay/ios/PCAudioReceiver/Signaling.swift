import Foundation
import RelayCore

enum SignalingError: LocalizedError {
    case unreachable
    case badKey
    case server(String)
    case invalidResponse

    var errorDescription: String? {
        switch self {
        case .unreachable:
            return "Can't reach the PC. Is the sender running, and is this iPhone on the same Wi-Fi?"
        case .badKey:
            return "The PC rejected the pairing key. Scan the QR code on the PC again."
        case .server(let message):
            return message
        case .invalidResponse:
            return "The PC sent a response this app doesn't understand. Update the sender."
        }
    }

    /// Retrying cannot fix a wrong key.
    var isFatal: Bool { if case .badKey = self { return true } else { return false } }
}

struct OfferResponse: Decodable {
    let sdp: String
    let type: String
    let sessionId: String?
}

private struct ErrorBody: Decodable { let message: String? }
private struct StatusBody: Decodable {
    struct Peer: Decodable { let reason: String? }
    let peer: Peer?
}

/// Plain HTTP signaling to the Windows sender (see docs/PROTOCOL.md).
struct SignalingClient {
    let target: RelayTarget

    private var session: URLSession {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 8
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }

    private func request(_ path: String, method: String = "GET", body: Data? = nil) -> URLRequest {
        var r = URLRequest(url: target.baseURL.appendingPathComponent(path))
        r.httpMethod = method
        r.cachePolicy = .reloadIgnoringLocalCacheData
        if let key = target.key { r.setValue(key, forHTTPHeaderField: "X-Relay-Key") }
        if let body { r.httpBody = body; r.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        return r
    }

    func sendOffer(_ sdp: String) async throws -> OfferResponse {
        let payload = try JSONSerialization.data(withJSONObject: ["sdp": sdp, "type": "offer"])
        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await session.data(for: request("api/offer", method: "POST", body: payload))
        } catch {
            throw SignalingError.unreachable
        }
        guard let http = response as? HTTPURLResponse else { throw SignalingError.invalidResponse }
        if http.statusCode == 401 { throw SignalingError.badKey }
        guard http.statusCode == 200 else {
            let message = (try? JSONDecoder().decode(ErrorBody.self, from: data))?.message
            throw SignalingError.server(message ?? "The PC returned an error (\(http.statusCode)).")
        }
        guard let decoded = try? JSONDecoder().decode(OfferResponse.self, from: data), decoded.type == "answer" else {
            throw SignalingError.invalidResponse
        }
        return decoded
    }

    func hangup(sessionId: String) {
        guard let body = try? JSONSerialization.data(withJSONObject: ["sessionId": sessionId]) else { return }
        let req = request("api/hangup", method: "POST", body: body)
        session.dataTask(with: req).resume()
    }

    /// Why the server thinks the audio connection is failing (firewall hint etc.), if it knows.
    func failureReason(sessionId: String) async -> String? {
        guard var comps = URLComponents(url: target.baseURL.appendingPathComponent("api/status"), resolvingAgainstBaseURL: false) else { return nil }
        comps.queryItems = [URLQueryItem(name: "sid", value: sessionId)]
        guard let url = comps.url,
              let (data, _) = try? await session.data(for: URLRequest(url: url)),
              let reason = (try? JSONDecoder().decode(StatusBody.self, from: data))?.peer?.reason,
              !reason.isEmpty else { return nil }
        return reason
    }
}
