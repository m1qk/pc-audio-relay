import asyncio

import numpy as np
import pytest

from pc_audio_relay.app import RelayApp
from pc_audio_relay.config import Settings
from tests.helpers import collect_pcm, connect_client, dominant_freq, free_port


@pytest.fixture
def relay(tmp_path):
    s = Settings(port=free_port(), key="testkey123", capture_buffer_ms=40).clamp()
    app = RelayApp(s, tone_mode="tone")
    app.start()
    yield app
    app.stop()


async def test_stereo_opus_arrives_and_decodes(relay):
    pc, track, body, status = await connect_client(relay.settings.port, relay.settings.key)
    try:
        assert status == 200
        assert "stereo=1" in body["sdp"], "answer must advertise stereo Opus"
        assert "opus/48000/2" in body["sdp"]
        assert "PCMU" not in body["sdp"], "answer should be Opus only"
        pcm = await collect_pcm(track, 2.5)
        pcm = pcm[48000 // 2:]  # skip the first half second (startup)
        left, right = pcm[:, 0], pcm[:, 1]
        assert abs(dominant_freq(left) - 440) < 8, dominant_freq(left)
        assert abs(dominant_freq(right) - 880) < 8, dominant_freq(right)
        assert np.abs(left).max() > 5000  # actually audible, not silence
    finally:
        await pc.close()


async def test_wrong_or_missing_key_is_rejected(relay):
    pc, track, body, status = await connect_client(relay.settings.port, "nope")
    assert status == 401 and body["error"] == "bad_key" and track is None
    pc2, track2, body2, status2 = await connect_client(relay.settings.port, None)
    assert status2 == 401
    assert relay.snapshot()["clients"] == []


async def test_new_connection_replaces_old_and_audio_keeps_flowing(relay):
    pc1, t1, _, s1 = await connect_client(relay.settings.port, relay.settings.key)
    assert s1 == 200
    await collect_pcm(t1, 0.4)
    first_sid = relay.snapshot()["clients"][0]["sessionId"]
    # phone "reconnects" (e.g. after screen unlock) without hanging up first
    pc2, t2, _, s2 = await connect_client(relay.settings.port, relay.settings.key)
    try:
        assert s2 == 200
        clients = relay.snapshot()["clients"]
        assert len(clients) == 1 and clients[0]["sessionId"] != first_sid
        pcm = (await collect_pcm(t2, 1.5))[48000 // 2:]
        assert abs(dominant_freq(pcm[:, 0]) - 440) < 8
    finally:
        await pc1.close()
        await pc2.close()


async def test_mdns_hostnames_in_offer_are_handled(relay):
    """Browsers hide their IP behind '<uuid>.local'. Simulate that by rewriting the
    client's real candidate IP to an unresolvable mDNS name and connecting via the
    LAN address so the server can substitute the HTTP peer's IP."""
    import aiohttp
    from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
    lan_ip = relay.address.ip
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    pc.addTransceiver("audio", direction="recvonly")
    got = asyncio.get_event_loop().create_future()
    pc.on("track", lambda t: got.set_result(t) if not got.done() else None)
    await pc.setLocalDescription(await pc.createOffer())
    sdp = pc.localDescription.sdp
    assert lan_ip in sdp, "test precondition: client gathered the LAN address"
    mdns_sdp = sdp.replace(lan_ip, "3f2c1d9e-1111-2222-3333-444455556666.local")
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://{lan_ip}:{relay.settings.port}/api/offer",
                          headers={"X-Relay-Key": relay.settings.key},
                          json={"sdp": mdns_sdp, "type": "offer"}) as r:
            assert r.status == 200
            body = await r.json()
    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=body["sdp"], type="answer"))
        track = await asyncio.wait_for(got, 10)
        pcm = (await collect_pcm(track, 1.5))[48000 // 2:]
        assert abs(dominant_freq(pcm[:, 0]) - 440) < 8
    finally:
        await pc.close()


async def _measured_kbps(app, seconds=3.0):
    pc, track, _, _ = await connect_client(app.settings.port, app.settings.key)
    try:
        async def drain():
            while True:
                await track.recv()
        task = asyncio.ensure_future(drain())
        await asyncio.sleep(1.0)
        peer = next(iter(app.server._peers.values()))

        async def sent():
            for st in (await peer.pc.getStats()).values():
                if st.type == "outbound-rtp":
                    return st.bytesSent
        b0 = await sent()
        await asyncio.sleep(seconds)
        b1 = await sent()
        task.cancel()
        return (b1 - b0) * 8 / seconds / 1000
    finally:
        await pc.close()


@pytest.mark.parametrize("kbps", [64, 160])
async def test_opus_bitrate_setting_is_honoured(kbps):
    s = Settings(port=free_port(), key="k", bitrate_kbps=kbps).clamp()
    app = RelayApp(s, tone_mode="noise")  # white noise needs the full bitrate
    app.start()
    try:
        measured = await _measured_kbps(app)
    finally:
        app.stop()
    # payload bitrate + RTP overhead (~12-30 bytes/20 ms => +10..20 kbps)
    assert kbps * 0.85 < measured < kbps + 30, f"asked {kbps} kbps, measured {measured:.0f} kbps"
