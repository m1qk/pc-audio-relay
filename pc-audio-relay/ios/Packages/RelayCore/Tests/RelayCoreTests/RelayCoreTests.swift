import XCTest
@testable import RelayCore

final class RelayCoreTests: XCTestCase {
    func testParsesQRPayload() {
        let t = RelayTarget.parse("http://192.168.1.20:8765/?k=abc123")
        XCTAssertEqual(t, RelayTarget(host: "192.168.1.20", port: 8765, key: "abc123"))
    }

    func testParsesHandTypedForms() {
        XCTAssertEqual(RelayTarget.parse("192.168.1.20"), RelayTarget(host: "192.168.1.20", port: 8765, key: nil))
        XCTAssertEqual(RelayTarget.parse("  192.168.1.20:9000 \n"), RelayTarget(host: "192.168.1.20", port: 9000, key: nil))
        XCTAssertEqual(RelayTarget.parse("my-pc.local:8765/?k=z")?.key, "z")
        XCTAssertEqual(RelayTarget.parse("192.168.1.20:8765/?k=")?.key, nil)
    }

    func testRejectsGarbage() {
        XCTAssertNil(RelayTarget.parse(""))
        XCTAssertNil(RelayTarget.parse("   "))
        XCTAssertNil(RelayTarget.parse("https://example.com"))
        XCTAssertNil(RelayTarget.parse("http://192.168.1.20:99999"))
        XCTAssertNil(RelayTarget.parse("not a url with spaces"))
    }

    func testBaseURLBracketsIPv6() {
        XCTAssertEqual(RelayTarget(host: "192.168.1.20", port: 8765).baseURL.absoluteString, "http://192.168.1.20:8765")
        XCTAssertEqual(RelayTarget(host: "fe80::1", port: 8765).baseURL.absoluteString, "http://[fe80::1]:8765")
    }

    func testBackoffMatchesSafariReceiver() {
        XCTAssertEqual(RelayCore.reconnectDelay(attempt: 1), 0.25)
        XCTAssertEqual(RelayCore.reconnectDelay(attempt: 2), 1)
        XCTAssertEqual(RelayCore.reconnectDelay(attempt: 3), 2)
        XCTAssertEqual(RelayCore.reconnectDelay(attempt: 4), 4)
        XCTAssertEqual(RelayCore.reconnectDelay(attempt: 5), 8)
        XCTAssertEqual(RelayCore.reconnectDelay(attempt: 50), 8)
    }

    private let offer = [
        "v=0", "m=audio 9 UDP/TLS/RTP/SAVPF 111 63",
        "a=rtpmap:111 opus/48000/2",
        "a=fmtp:111 minptime=10;useinbandfec=1",
        "a=rtpmap:63 red/48000/2",
    ].joined(separator: "\r\n") + "\r\n"

    func testStereoAddedToExistingFmtp() {
        let out = RelayCore.addOpusStereo(offer)
        XCTAssertTrue(out.contains("a=fmtp:111 minptime=10;useinbandfec=1;stereo=1;sprop-stereo=1"))
        XCTAssertTrue(out.hasSuffix("\r\n"))
    }

    func testStereoIsIdempotent() {
        let once = RelayCore.addOpusStereo(offer)
        XCTAssertEqual(RelayCore.addOpusStereo(once), once)
    }

    func testStereoCreatesFmtpWhenMissing() {
        let sdp = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 96\r\na=rtpmap:96 opus/48000/2\r\n"
        XCTAssertTrue(RelayCore.addOpusStereo(sdp).contains("a=fmtp:96 minptime=10;stereo=1;sprop-stereo=1"))
    }
}
