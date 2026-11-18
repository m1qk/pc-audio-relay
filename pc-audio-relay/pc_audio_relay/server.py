"""HTTP signaling + web receiver, and the WebRTC peer management.

Endpoints (also documented in docs/PROTOCOL.md):
    GET  /                 web receiver (Safari)
    GET  /api/info         identity + capabilities (no key needed)
    POST /api/offer        {sdp,type:"offer"} -> {sdp,type:"answer",sessionId}
    POST /api/hangup       {sessionId}
    GET  /api/status       server's view of the session, for diagnosing failures
Everything stays on the local network: no STUN/TURN, no cloud, no account.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from aiohttp import web
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

from . import sdp as sdputil
from .audio import PcmRing
from .config import APP_NAME, CHANNELS, PROTOCOL_VERSION, SAMPLE_RATE, VERSION, Settings
from .track import RelayAudioTrack

CONNECT_TIMEOUT_S = 10.0

UDP_HINT = (
    "The phone reached the PC over HTTP, but the audio connection (UDP) did not complete. "
    "Most likely causes: Windows Firewall is blocking this app on UDP, the PC's network is set to "
    "'Public' instead of 'Private', a VPN or virtual adapter is being advertised, or the Wi-Fi "
    "isolates devices from each other (guest/campus/hotel networks)."
)


@dataclass
class Peer:
    sid: str
    pc: RTCPeerConnection
    remote_ip: str
    track: RelayAudioTrack
    created: float
    connection_state: str = "new"
    ice_state: str = "new"
    reason: str = ""
    connected_at: Optional[float] = None
    watch: Optional[asyncio.Future] = None


class RelayServer:
    def __init__(self, ring: PcmRing, engine, settings: Settings, web_root: Path,
                 advertise_ip: Optional[str] = None,
                 on_event: Optional[Callable[[str, str], None]] = None):
        self.ring = ring
        self.engine = engine
        self.settings = settings
        self.web_root = Path(web_root)
        self.advertise_ip = advertise_ip
        self._on_event = on_event or (lambda kind, msg: None)
        self._peers: dict[str, Peer] = {}
        self._runner: Optional[web.AppRunner] = None
        self.offers_received = 0
        self.rejected_offers = 0
        self.last_offer_from: Optional[str] = None

    # ------------------------------------------------------------------ #
    def _emit(self, kind: str, msg: str) -> None:
        try:
            self._on_event(kind, msg)
        except Exception:
            pass

    async def start(self) -> None:
        app = web.Application(middlewares=[self._middleware], client_max_size=128 * 1024)
        app.router.add_get("/", self._index)
        app.router.add_get("/api/info", self._info)
        app.router.add_post("/api/offer", self._offer)
        app.router.add_post("/api/hangup", self._hangup)
        app.router.add_get("/api/status", self._status)
        app.router.add_static("/assets/", self.web_root, show_index=False)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.settings.port)
        await site.start()
        self._emit("log", f"listening on 0.0.0.0:{self.settings.port}")

    async def stop(self) -> None:
        await self._close_all("server stopping")
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------------ #
    @web.middleware
    async def _middleware(self, request: web.Request, handler):
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        if isinstance(resp, web.HTTPException):
            raise resp
        return resp

    def _authorized(self, request: web.Request) -> bool:
        if not self.settings.require_key:
            return True
        supplied = request.headers.get("X-Relay-Key") or request.query.get("k") or ""
        return secrets.compare_digest(supplied.encode(), self.settings.key.encode())

    async def _index(self, request: web.Request):
        return web.FileResponse(self.web_root / "index.html")

    async def _info(self, request: web.Request):
        return web.json_response({
            "app": APP_NAME,
            "version": VERSION,
            "protocol": PROTOCOL_VERSION,
            "host": socket.gethostname(),
            "requiresKey": self.settings.require_key,
            "codec": "opus",
            "sampleRate": SAMPLE_RATE,
            "channels": CHANNELS,
            "device": self.engine.device_name,
            "captureState": self.engine.state,
        })

    async def _offer(self, request: web.Request):
        if not self._authorized(request):
            self.rejected_offers += 1
            self._emit("warn", f"{request.remote}: rejected, wrong or missing pairing key (scan the QR code again)")
            return web.json_response(
                {"error": "bad_key", "message": "Missing or wrong pairing key. Scan the QR code again."},
                status=401)
        try:
            params = await request.json()
            offer_sdp = params["sdp"]
            if params.get("type") != "offer" or not isinstance(offer_sdp, str):
                raise ValueError("expected an SDP offer")
        except Exception as e:
            return web.json_response({"error": "bad_request", "message": str(e)}, status=400)

        remote_ip = request.remote or ""
        self.offers_received += 1
        self.last_offer_from = remote_ip
        self._emit("log", f"connection request from {remote_ip}")

        # One listener at a time: a new request replaces the old connection. This
        # is what makes phone reconnects (screen unlock, Wi-Fi roam) clean.
        await self._close_all("replaced by a new connection")

        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
        track = RelayAudioTrack(self.ring, prefill_ms=self.settings.capture_buffer_ms)
        peer = Peer(sid=secrets.token_urlsafe(8), pc=pc, remote_ip=remote_ip, track=track,
                    created=time.time())
        self._peers[peer.sid] = peer

        @pc.on("connectionstatechange")
        async def _on_connection_state():
            peer.connection_state = pc.connectionState
            if pc.connectionState == "connected":
                peer.connected_at = time.time()
                peer.reason = ""
                self._emit("client", f"connected: {remote_ip}")
            elif pc.connectionState == "failed":
                peer.reason = peer.reason or UDP_HINT
                self._emit("warn", f"connection to {remote_ip} failed. {UDP_HINT}")
            elif pc.connectionState == "closed":
                self._emit("client", f"disconnected: {remote_ip}")
            if pc.connectionState in ("failed", "closed"):
                await self._drop(peer.sid)

        @pc.on("iceconnectionstatechange")
        async def _on_ice_state():
            peer.ice_state = pc.iceConnectionState

        try:
            sanitized = sdputil.rewrite_mdns_candidates(offer_sdp, remote_ip)
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sanitized, type="offer"))
            audio = [t for t in pc.getTransceivers() if t.kind == "audio"]
            if not audio:
                raise ValueError("the offer has no audio section")
            pc.addTrack(track)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            answer_sdp = sdputil.add_opus_stereo(sdputil.keep_only_opus(pc.localDescription.sdp))
            if self.advertise_ip:
                answer_sdp = sdputil.filter_local_candidates(answer_sdp, {self.advertise_ip})
        except Exception as e:
            await self._drop(peer.sid)
            self._emit("warn", f"could not answer {remote_ip}: {e}")
            return web.json_response({"error": "bad_offer", "message": str(e)}, status=400)

        self._emit("log", "answering with candidates: " + ", ".join(sdputil.list_candidates(answer_sdp)))
        peer.watch = asyncio.ensure_future(self._watch_connect(peer))
        return web.json_response({
            "sdp": answer_sdp,
            "type": "answer",
            "sessionId": peer.sid,
            "protocol": PROTOCOL_VERSION,
        })

    async def _watch_connect(self, peer: Peer) -> None:
        await asyncio.sleep(CONNECT_TIMEOUT_S)
        if peer.sid in self._peers and peer.connection_state != "connected":
            peer.reason = UDP_HINT
            self._emit("warn", f"{peer.remote_ip}: no audio connection after {CONNECT_TIMEOUT_S:.0f}s. {UDP_HINT}")

    async def _hangup(self, request: web.Request):
        try:
            data = json.loads(await request.text() or "{}")
        except ValueError:
            data = {}
        sid = data.get("sessionId")
        if sid in self._peers:
            await self._drop(sid)
        return web.json_response({"ok": True})

    async def _status(self, request: web.Request):
        peer = self._peers.get(request.query.get("sid", ""))
        body = {"capture": self.capture_status(), "peer": self._peer_status(peer) if peer else None}
        return web.json_response(body)

    # ------------------------------------------------------------------ #
    async def _drop(self, sid: str) -> None:
        peer = self._peers.pop(sid, None)
        if peer is not None:
            if peer.watch is not None and peer.watch is not asyncio.current_task():
                peer.watch.cancel()
            try:
                peer.track.stop()
                await peer.pc.close()
            except Exception:
                pass

    async def _close_all(self, why: str) -> None:
        for sid in list(self._peers):
            self._emit("log", f"closing {self._peers[sid].remote_ip}: {why}")
            await self._drop(sid)

    def capture_status(self) -> dict:
        return {
            "state": self.engine.state,
            "detail": self.engine.state_detail,
            "device": self.engine.device_name,
            "level": round(float(self.engine.level), 3),
            "queuedMs": round(self.ring.level() * 1000 / SAMPLE_RATE, 1),
            "droppedMs": round(self.ring.dropped_samples * 1000 / SAMPLE_RATE),
        }

    def _peer_status(self, p: Peer) -> dict:
        return {
            "sessionId": p.sid,
            "remote": p.remote_ip,
            "connectionState": p.connection_state,
            "iceState": p.ice_state,
            "reason": p.reason,
            "framesSent": p.track.frames_sent,
            "underruns": p.track.underruns,
            "driftDrops": p.track.drift_drops,
        }

    def snapshot(self) -> dict:
        return {
            "capture": self.capture_status(),
            "clients": [self._peer_status(p) for p in self._peers.values()],
            "offers": self.offers_received,
            "rejected": self.rejected_offers,
            "lastOfferFrom": self.last_offer_from,
        }
