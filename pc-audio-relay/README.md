# PC Audio Relay

Stream your Windows 11 PC's system audio to your iPhone over your home Wi-Fi. Audio from games, videos, music and any other app plays on the iPhone speaker or on headphones connected to it.

* **Windows sender:** captures system audio with WASAPI loopback, encodes stereo Opus, and streams it over WebRTC. No cloud, no account, no relay server.
* **Safari receiver (works today):** the sender serves a web page. Scan the QR code, tap Connect.
* **Native SwiftUI receiver (needs a Mac or a cloud Mac to build):** written, not yet built. See [docs/IOS_INSTALL.md](docs/IOS_INSTALL.md).

## What has and hasn't been verified

I built this in a Linux workspace with no Windows PC, no iPhone and no Mac. So there are two kinds of claims below.

| Area | Status |
|---|---|
| Sender core: ring buffer, pacing, Opus stereo, SDP handling, pairing key, reconnects | **Tested** (63 automated tests, Python 3.12 and 3.13) |
| WebRTC end to end: sender to real Chrome, stereo 440/880 Hz verified on separate channels, reconnect after dropped session and after sender restart | **Tested** (headless Chrome 153) |
| Windows GUI: device list, Start/Stop, status, QR code (decoded back to the URL), address list, log, device switching while running | **Tested** (real Tk window under a virtual display, with a fake audio device) |
| Opus bitrate setting, capture device switching, device unplug and resume, 5.1 downmix, 44.1 kHz resampling | **Tested** (against a fake audio backend) |
| All dependencies install from prebuilt Windows wheels (no compiler) | **Checked** (resolved for win_amd64, Python 3.12) |
| Delay analysis tool recovers known delays (38, 85.5, 240 ms) | **Tested** on synthetic recordings |
| **WASAPI loopback capture on your Dell XPS 8940** | **Needs your PC.** Written against PyAudioWPatch's documented API; never run on Windows. |
| **Windows Firewall prompt, network profile, firewall fix button** | **Needs your PC.** Logic tested with canned command output only. |
| **Safari behaviour on your iPhone** (autoplay, stereo, volume, route changes) | **Needs your iPhone.** Page verified in Chrome only. |
| **Screen lock and background playback in Safari** | **Needs your iPhone.** Unknown until you test. |
| **Real end-to-end delay** | **Needs your devices.** Software floor measured, see below. |
| **Native iOS app** | **Syntax-checked only** (Swift grammar parse, zero errors). Never compiled or run. |

## Set up and launch (Windows 11)

You need Python 3.12 (the version everything here was tested with).

```powershell
winget install Python.Python.3.12
```

Close and reopen PowerShell, then in the project folder:

```powershell
.\setup.bat      # one time: creates .venv and installs dependencies
.\start.bat      # opens the app
```

Or by hand:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pc_audio_relay
```

Other commands:

```powershell
.venv\Scripts\python -m pc_audio_relay --list-devices   # show playback devices
.venv\Scripts\python -m pc_audio_relay --diagnose       # network and firewall checks
.venv\Scripts\python -m pc_audio_relay --fix-firewall   # adds firewall allow rules (UAC prompt)
.venv\Scripts\python -m pc_audio_relay --console        # no window, QR code in the console
.venv\Scripts\python -m pc_audio_relay --tone           # stream a test tone (no PC audio needed)
```

### First run on Windows

1. Press **Start**. Windows Firewall will probably ask about Python. **Tick "Private networks" and click Allow.** If you cancelled, press **Fix firewall** in the app.
2. Leave the output device on **Follow Windows default output**, or pick one. Play something on the PC; the **Level** bar should move.
3. On the iPhone (same Wi-Fi), open the **Camera** app and scan the QR code, then tap the link.
4. Tap **Connect** on the page. You should hear the PC's audio.

The pairing key in the address (`?k=...`) stops other people on your Wi-Fi from listening. Keep the QR code to yourself.

## Settings

| Setting | Where | Default | Effect |
|---|---|---|---|
| Output device | window | follow Windows default | Which playback device's audio is shared. Following the default means plugging in headphones on the PC moves the capture automatically. |
| Buffer (ms) | window / `--buffer` | 40 | Sender-side buffer. Lower is less delay, higher survives Wi-Fi hiccups. 20 to 40 for gaming, 80 to 150 on a weak network. |
| Bitrate (kbps) | window / `--bitrate` | 128 | Opus bitrate. 128 is transparent for most music. Measured: the setting is honoured. |
| Port | window / `--port` | 8765 | HTTP port. |
| Pairing key | window | on | Leave on. |
| Receiver buffer | the page, under "Latency & stats" | 60 ms | Phone-side jitter buffer, adjustable live. Safari may not allow changing it; the page tells you. |

## Troubleshooting

Press **Diagnose** in the app (or run `--diagnose`). It reports the most likely cause and the fix.

* **Page won't open on the iPhone:** wrong network (guest Wi-Fi, mobile data, VPN on either device), or the router has "AP/client isolation" turned on. Check the address in the QR code matches a Wi-Fi address on the PC.
* **Page opens but stays on "Reconnecting":** the HTTP part works but UDP audio is blocked. Almost always Windows Firewall (use **Fix firewall**) or the PC's network is set to **Public** (Settings, Network & internet, Wi-Fi, your network, Network profile type, **Private**).
* **PC has several networks (VPN, WSL, Hyper-V):** pick the Wi-Fi address from the address list in the window. Virtual adapters are labelled.
* **No sound but connected:** check the Level bar. If it is flat, the selected device isn't the one playing sound. Pick it, or switch back to "Follow Windows default output".
* **Crackles:** raise the buffer (sender 80, receiver 100), move closer to the router, or use 5 GHz Wi-Fi.
* **Port in use:** change the port.

## Test checklist for your iPhone

Tick these in order. Write down what you see, especially for the lock-screen items, because I could not verify those.

**Playback**
- [ ] Windows: Start, play a YouTube video. Level bar moves.
- [ ] iPhone: scan QR, open link, tap Connect. Status turns green and you hear the video.
- [ ] Stereo: play a left/right channel test video. Left sounds on the left, right on the right (use headphones).
- [ ] iPhone speaker, then wired or Bluetooth headphones: audio follows the headphones when you connect them.
- [ ] Silent switch: with the ringer switch on silent, does audio still play? (Record the answer.)
- [ ] Volume: the side buttons change the volume. (The page's volume slider is hidden on iPhone because Safari ignores it.)

**Latency** (full method in [docs/LATENCY.md](docs/LATENCY.md))
- [ ] Open "Latency & stats" and note the estimated delay and the packet loss.
- [ ] Lip-sync check: video on the PC, audio on the iPhone. Is it noticeable? Try sender buffer 20 and receiver buffer 0.
- [ ] Click test with a recording: run the measurement and note the number for speaker and for headphones.

**Screen lock and background (Safari: verify separately)**
- [ ] Lock the phone with the side button while audio plays. Audio continues for 60 seconds? Yes / No / Stops after ___ s.
- [ ] Let the screen auto-lock instead of pressing the button. Same result?
- [ ] Switch to another app (Home swipe). Audio continues?
- [ ] Switch to another Safari tab. Audio continues?
- [ ] Repeat all of the above after **Share, Add to Home Screen**, opened from the home-screen icon. Standalone web apps often behave differently.
- [ ] If Safari stops audio while locked: that is an iOS limit for web pages, not a bug here. Use the native app ([docs/IOS_INSTALL.md](docs/IOS_INSTALL.md)), which has the background-audio mode that makes lock-screen playback work.

**Reconnects**
- [ ] Windows: press Stop, wait 5 s, press Start. The iPhone reconnects by itself within about 10 s.
- [ ] iPhone: turn Wi-Fi off for 5 s and back on. It reconnects.
- [ ] iPhone: lock for 30 s, unlock. It reconnects and plays, or tap "Tap to resume audio".
- [ ] Windows: plug in headphones (or change the default device). The sender log says it switched; audio keeps flowing.
- [ ] Windows: unplug USB or Bluetooth headphones while following the default. Capture moves to the speakers.
- [ ] Take a phone call or play a video elsewhere, then come back. Audio resumes.
- [ ] Close the sender while connected. The page shows a clear message and recovers when you start it again.

Measured recovery time in headless Chrome: about 5.6 s after a dropped session and about 5.6 s after a sender restart.

## Latency

Measured software pipeline only (sender, Opus, RTP, browser jitter buffer), on loopback with real Chrome. It does **not** include Wi-Fi, the iPhone's output hardware, or Bluetooth, so real delay will be higher. Safari's jitter buffer may differ from Chrome's.

| Sender buffer | Receiver buffer | Delay |
|---|---|---|
| 20 ms | 0 ms | about 103 ms |
| 40 ms | 0 ms | about 123 ms |
| 40 ms (default) | 60 ms (default) | about 148 ms |
| 80 ms | 60 ms | about 197 ms |
| 160 ms | 120 ms | about 332 ms |

Reproduce with `python tools/loopback_latency_probe.py` (needs Playwright and Chrome). For the real end-to-end number on your devices, follow [docs/LATENCY.md](docs/LATENCY.md).

## Project layout

```
pc_audio_relay/   sender: capture (wasapi.py), ring buffer + Opus track, WebRTC server, GUI, diagnostics
web/              Safari receiver page (app.js, sdp-utils.js, style.css)
ios/              native SwiftUI receiver + shared RelayCore package + project.yml
tools/            click track, delay analyzer, loopback latency probe
tests/            63 automated tests
docs/             PROTOCOL.md, LATENCY.md, IOS_INSTALL.md
.github/          workflow that builds the iPhone app on a cloud Mac
```

USB streaming is intentionally not in this version.

## Security notes

* Everything stays on your local network. There is no STUN or TURN server and no internet traffic.
* Audio is encrypted (WebRTC DTLS-SRTP). The page and signaling are plain HTTP on your LAN, so the pairing key travels unencrypted inside your own network. It keeps casual neighbours out; it is not protection against someone already sniffing your Wi-Fi.
* Only one listener at a time. A new connection replaces the old one.
