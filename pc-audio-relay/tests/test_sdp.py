from pc_audio_relay import sdp

OFFER = "\r\n".join([
    "v=0", "o=- 1 2 IN IP4 127.0.0.1", "s=-", "t=0 0",
    "m=audio 9 UDP/TLS/RTP/SAVPF 111 63 9 0 8",
    "c=IN IP4 0.0.0.0",
    "a=rtpmap:111 opus/48000/2",
    "a=rtcp-fb:111 transport-cc",
    "a=fmtp:111 minptime=10;useinbandfec=1",
    "a=rtpmap:9 G722/8000", "a=rtpmap:0 PCMU/8000", "a=rtpmap:8 PCMA/8000",
    "a=candidate:1 1 udp 2113937151 4a1b2c3d-aaaa-bbbb-cccc-0123456789ab.local 54321 typ host generation 0",
    "a=candidate:2 1 udp 2113937151 9f9f9f9f-aaaa-bbbb-cccc-0123456789ab.local 54321 typ host generation 0",
    "a=candidate:3 1 udp 1677729535 203.0.113.9 40000 typ srflx raddr 0.0.0.0 rport 0",
    "",
])


def test_mdns_candidates_become_the_http_peer_ip_and_dedupe():
    out = sdp.rewrite_mdns_candidates(OFFER, "192.168.1.23")
    assert ".local" not in out
    lines = [l for l in out.split("\r\n") if l.startswith("a=candidate")]
    # two mDNS names (e.g. IPv4+IPv6 of the phone) collapse into one, plus the srflx line
    assert len(lines) == 2
    assert "192.168.1.23 54321 typ host" in lines[0]
    assert out.endswith("\r\n")


def test_mdns_candidates_dropped_when_peer_not_ipv4():
    out = sdp.rewrite_mdns_candidates(OFFER, "fe80::1%en0")
    assert ".local" not in out
    assert "203.0.113.9" in out


def test_filter_keeps_only_advertised_ip_but_never_empties():
    sdp_in = "\r\n".join(["v=0", "a=candidate:1 1 udp 1 192.168.1.5 5000 typ host",
                          "a=candidate:2 1 udp 1 172.20.0.1 5001 typ host", "a=end-of-candidates", ""])
    out = sdp.filter_local_candidates(sdp_in, {"192.168.1.5"})
    assert "192.168.1.5" in out and "172.20.0.1" not in out and "end-of-candidates" in out
    # wrong address configured => fall back to advertising everything
    out2 = sdp.filter_local_candidates(sdp_in, {"10.9.9.9"})
    assert "192.168.1.5" in out2 and "172.20.0.1" in out2


def test_stereo_added_to_existing_fmtp():
    out = sdp.add_opus_stereo(OFFER)
    assert "a=fmtp:111 minptime=10;useinbandfec=1;stereo=1;sprop-stereo=1" in out
    assert out.count("stereo=1") == 2  # stereo=1 and sprop-stereo=1, once each
    assert sdp.add_opus_stereo(out) == out  # idempotent


def test_stereo_added_when_no_fmtp_line():
    s = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 96\r\na=rtpmap:96 opus/48000/2\r\n"
    out = sdp.add_opus_stereo(s)
    assert "a=fmtp:96 minptime=10;stereo=1;sprop-stereo=1" in out


def test_keep_only_opus():
    out = sdp.keep_only_opus(OFFER)
    assert "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n" in out
    assert "PCMU" not in out and "G722" not in out and "a=rtpmap:111 opus/48000/2" in out
    assert "a=fmtp:111" in out and "a=rtcp-fb:111" in out
