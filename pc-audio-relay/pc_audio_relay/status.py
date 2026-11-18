"""Turns the server snapshot into one human-readable status line.

Pure function, no GUI imports, so it is unit-tested and shared by the Tk
window and the console mode.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

NO_PHONE_HINT_AFTER_S = 25


@dataclass
class Status:
    level: str      # idle | waiting | connecting | ok | warn | error
    text: str       # short headline
    detail: str = ""


def compute_status(running: bool, snap: dict, uptime_s: float = 0.0) -> Status:
    if not running:
        return Status("idle", "Stopped", "Press Start to begin sharing this PC's audio.")

    cap = snap.get("capture", {}) or {}
    cstate = cap.get("state", "starting")
    cdetail = cap.get("detail", "")
    if cstate == "error":
        return Status("error", "Audio capture problem", cdetail)
    if cstate == "waiting":
        return Status("warn", "Waiting for an audio device", cdetail)

    clients = snap.get("clients") or []
    dev = cap.get("device") or ""
    if clients:
        c = clients[0]
        st = c.get("connectionState")
        if st == "connected":
            extra = ""
            if c.get("underruns", 0) > 20:
                extra = " Audio is breaking up: try a larger buffer or check the Wi-Fi signal."
            return Status("ok", f"Streaming to {c['remote']}", f"Capturing: {dev}.{extra}")
        if st == "failed" or c.get("reason"):
            return Status("error", f"{c['remote']} could not connect",
                          c.get("reason") or "The audio connection failed.")
        return Status("connecting", f"{c['remote']} is connecting", "Setting up the audio connection…")

    if snap.get("offers", 0) == 0 and snap.get("rejected", 0) == 0 and uptime_s > NO_PHONE_HINT_AFTER_S:
        return Status("waiting", "Waiting for your iPhone",
                      "No phone has reached this PC yet. Check that the iPhone is on the same Wi-Fi "
                      "and use Diagnose if it still can't open the page.")
    if snap.get("rejected", 0) and snap.get("offers", 0) == 0:
        return Status("warn", "Pairing key rejected",
                      "A device tried to connect with the wrong key. Scan the QR code again.")
    return Status("waiting", "Ready", f"Waiting for your iPhone. Capturing: {dev or 'starting…'}")
