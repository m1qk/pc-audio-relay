"""Shared helpers: a Python stand-in for the browser, and signal analysis."""
from __future__ import annotations

import asyncio
import socket
import time

import aiohttp
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def connect_client(port: int, key: str | None, host: str = "127.0.0.1", extra_headers=None):
    """Behaves like the web page: recvonly audio offer, POST /api/offer, apply answer."""
    pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
    pc.addTransceiver("audio", direction="recvonly")
    track_future: asyncio.Future = asyncio.get_event_loop().create_future()

    @pc.on("track")
    def on_track(track):
        if not track_future.done():
            track_future.set_result(track)

    await pc.setLocalDescription(await pc.createOffer())
    headers = {"X-Relay-Key": key} if key else {}
    headers.update(extra_headers or {})
    async with aiohttp.ClientSession() as s:
        async with s.post(f"http://{host}:{port}/api/offer", headers=headers,
                          json={"sdp": pc.localDescription.sdp, "type": "offer"}) as r:
            body = await r.json()
            status = r.status
    if status != 200:
        await pc.close()
        return pc, None, body, status
    await pc.setRemoteDescription(RTCSessionDescription(sdp=body["sdp"], type="answer"))
    track = await asyncio.wait_for(track_future, 10)
    return pc, track, body, status


async def collect_pcm(track, seconds: float) -> np.ndarray:
    """Read decoded frames for `seconds`; returns (n, 2) int16."""
    chunks = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        frame = await asyncio.wait_for(track.recv(), 5)
        arr = frame.to_ndarray()  # packed s16 -> (1, n*channels)
        chunks.append(arr.reshape(-1, frame.layout.nb_channels if hasattr(frame.layout, "nb_channels") else 2))
    return np.concatenate(chunks)


def dominant_freq(x: np.ndarray, rate: int = 48000) -> float:
    x = x.astype(np.float64)
    x = x - x.mean()
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / rate)[int(spec.argmax())])
