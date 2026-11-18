"""Runs the real Tk window (needs tkinter + a display; use xvfb-run on Linux).

A fake audio backend feeds a 440/880 Hz tone through the real CaptureManager,
so this covers window -> RelayApp -> capture -> server -> WebRTC client.
"""
import asyncio
import os
import threading
import time

import numpy as np
import pytest

tk = pytest.importorskip("tkinter")
if not os.environ.get("DISPLAY"):
    pytest.skip("no display (run under xvfb-run)", allow_module_level=True)

from pc_audio_relay.config import Settings  # noqa: E402
from pc_audio_relay.gui import FOLLOW_DEFAULT, MainWindow  # noqa: E402
from tests.helpers import collect_pcm, connect_client, dominant_freq, free_port  # noqa: E402
from tests.test_capture_manager import FakeBackend  # noqa: E402


class FeedingBackend(FakeBackend):
    """FakeBackend that also pushes a stereo test tone into every opened stream."""

    def open(self, name, on_pcm):
        s = super().open(name, on_pcm)
        stop = threading.Event()

        def feed():
            n, t0 = 0, time.monotonic()
            while not stop.is_set():
                idx = np.arange(n, n + 480)
                l = (np.sin(2 * np.pi * 440 * idx / 48000) * 9000).astype(np.int16)
                r = (np.sin(2 * np.pi * 880 * idx / 48000) * 9000).astype(np.int16)
                s.feed(np.stack([l, r], 1).tobytes())
                n += 480
                d = t0 + n / 48000 - time.monotonic()
                if d > 0:
                    time.sleep(d)

        th = threading.Thread(target=feed, daemon=True)
        th.start()
        orig_close = s.close
        s.close = lambda: (stop.set(), orig_close())
        return s


def pump(root, seconds=0.0, until=None, timeout=10.0):
    end = time.time() + (timeout if until else seconds)
    while time.time() < end:
        root.update()
        if until and until():
            return True
        time.sleep(0.02)
    return not until


@pytest.fixture
def win(tmp_path):
    root = tk.Tk()
    s = Settings(port=free_port(), key="guikey", capture_buffer_ms=40).clamp()
    backend = FeedingBackend(["Speakers", "Headphones"], default="Speakers")
    w = MainWindow(root, s, backend=backend)
    yield w, backend
    try:
        if w.app and w.app.running:
            w.app.stop()
    finally:
        root.destroy()


def test_window_lists_devices_and_shows_qr_and_address(win):
    w, _ = win
    assert pump(w.root, until=lambda: len(w.device_box.cget("values")) == 3)
    assert list(w.device_box.cget("values")) == [FOLLOW_DEFAULT, "Speakers", "Headphones"]
    assert w.device_var.get() == FOLLOW_DEFAULT
    assert "Speakers" in w.device_note.cget("text")
    url = w.url_var.get()
    assert url.startswith("http://") and f":{w.settings.port}/?k=guikey" in url
    assert len(w.qr.find_all()) > 100, "QR modules should be drawn on the canvas"
    assert w.status_var.get() == "Stopped"


def test_start_stream_switch_device_stop(win):
    w, backend = win
    pump(w.root, until=lambda: len(w.device_box.cget("values")) == 3)
    w.toggle()
    assert w.toggle_btn.cget("text") == "Stop"
    assert pump(w.root, until=lambda: w.status_var.get() == "Ready"), w.status_var.get()

    # a phone connects while the window is open
    result = {}
    release = threading.Event()

    def client():
        async def go():
            pc, track, body, status = await connect_client(w.settings.port, w.settings.key, host=w.app.address.ip)
            result["status"] = status
            pcm = (await collect_pcm(track, 2.0))[24000:]
            result["f"] = (dominant_freq(pcm[:, 0]), dominant_freq(pcm[:, 1]))
            while not release.is_set():  # stay connected while the main thread inspects the window
                await asyncio.sleep(0.05)
            await pc.close()
        asyncio.run(go())

    th = threading.Thread(target=client)
    th.start()
    assert pump(w.root, until=lambda: "f" in result, timeout=25)
    assert pump(w.root, until=lambda: w.status_var.get().startswith("Streaming to"), timeout=5), w.status_var.get()
    assert "Speakers" in w.detail_var.get()
    release.set()
    th.join(5)
    assert result.get("status") == 200
    assert abs(result["f"][0] - 440) < 8 and abs(result["f"][1] - 880) < 8, result

    # choose a different output device while running
    w.device_var.set("Headphones")
    w.on_device_selected()
    assert pump(w.root, until=lambda: w.app.engine.device_name == "Headphones", timeout=8)

    w.toggle()
    assert w.toggle_btn.cget("text") == "Start" and str(w.toggle_btn.cget("state")) == "normal"
    assert w.status_var.get() == "Stopped"
    assert not w.app.running


def test_busy_port_shows_error_instead_of_crashing(win, monkeypatch):
    import socket
    from tkinter import messagebox
    w, _ = win
    shown = []
    monkeypatch.setattr(messagebox, "showerror", lambda title, msg: shown.append(msg))
    blocker = socket.socket()
    blocker.bind(("0.0.0.0", w.settings.port))
    blocker.listen(1)
    try:
        w.start()
    finally:
        blocker.close()
    assert shown and "port" in shown[0].lower()
    assert w.toggle_btn.cget("text") == "Start"
    assert w.app is None


def test_diagnose_writes_to_log(win):
    w, _ = win
    w.diagnose()
    assert pump(w.root, until=lambda: "Network" in w.log.get("1.0", "end"), timeout=10)
