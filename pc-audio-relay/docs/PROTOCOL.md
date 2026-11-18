# Signaling protocol (version 1)

The Windows sender serves everything over plain HTTP on the local network. The Safari page and the native iPhone app speak exactly the same protocol, so a receiver is just "make an offer, post it, apply the answer".

## Authentication

The QR code contains `http://<pc-ip>:<port>/?k=<key>`. The key is sent back as the `X-Relay-Key` header (or `?k=` query parameter) on `POST /api/offer`. A missing or wrong key gives `401` and `{"error":"bad_key","message":"..."}`. A receiver should **not** retry after a 401.

## Endpoints

### `GET /api/info` (no key needed)
```json
{"app":"PC Audio Relay","version":"0.1.0","protocol":1,"host":"DESKTOP-ABC","requiresKey":true,
 "codec":"opus","sampleRate":48000,"channels":2,"device":"Speakers (Realtek)","captureState":"running"}
```

### `POST /api/offer`
Request: `{"sdp": "<offer SDP>", "type": "offer"}`

The offer must contain an audio m-line (receivers should make it `recvonly`; the sender only sends). Offer Opus; the answer contains **only** Opus (`opus/48000/2`, `stereo=1;sprop-stereo=1`). ICE servers are not needed or used; the answer carries the PC's LAN host candidate only.

Response `200`: `{"sdp":"<answer SDP>","type":"answer","sessionId":"abc123","protocol":1}`

Errors: `400 {"error":"bad_request"|"bad_offer","message":...}`, `401 bad_key`.

Notes for receivers:
* Browsers hide their address behind `<uuid>.local` (mDNS). The sender replaces those with the IP the HTTP request came from, so receivers need no special handling.
* Add `stereo=1;sprop-stereo=1` to the Opus `fmtp` line of your offer, or the browser decodes mono. (`RelayCore.addOpusStereo` and `PCAudioSdp.addOpusStereo` do this.)
* Only **one listener** at a time. A new offer closes the previous connection. This makes reconnecting after a Wi-Fi roam or screen unlock a plain "post a new offer".

### `POST /api/hangup`
`{"sessionId":"abc123"}` closes that session. Always `200`. Also sent with `sendBeacon` when the page closes.

### `GET /api/status?sid=<sessionId>`
```json
{"capture":{"state":"running","detail":"","device":"Speakers","level":0.21,"queuedMs":40.0,"droppedMs":0},
 "peer":{"sessionId":"abc123","remote":"192.168.1.23","connectionState":"connected","iceState":"completed",
         "reason":"","framesSent":1500,"underruns":0,"driftDrops":0}}
```
`peer.reason` is a human-readable explanation (for example the firewall hint) when the audio connection fails or times out. Receivers show it to the user.

## Receiver behaviour that the sender relies on

* Reconnect with backoff: 0.25 s, 1 s, 2 s, 4 s, then 8 s. (The sender keeps sending silence frames continuously, so 2.5 s without RTP packets means the connection is really broken.)
* Treat WebRTC `disconnected` as possibly temporary: wait about 3 s before reconnecting.
* Treat `failed`, no connection within 10 s, or 2.5 s without packets as a reason to reconnect.

## Audio format

48 kHz stereo Opus, 20 ms frames, `application=audio`, variable bitrate (default 128 kbps). The sender paces frames on the system clock and fills gaps with silence, because Windows loopback delivers no data while nothing is playing.
