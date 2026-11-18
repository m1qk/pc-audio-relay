"""Orchestrates capture engine + server on a background asyncio loop.

Used by both the tkinter GUI and the console mode. Thread-safe: the GUI thread
calls start()/stop()/set_device() and drains `events`.
"""
from __future__ import annotations

import asyncio
import queue
import sys
import threading
from pathlib import Path
from typing import Optional

from .audio import CaptureManager, PcmRing, ToneCapture
from .codec import configure_opus
from .config import Settings
from .netinfo import LanAddress, build_url, hostname_url, pick_address
from .server import RelayServer

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


def _frozen_web_root() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / "web" if base else WEB_ROOT


class StartError(RuntimeError):
    pass


class RelayApp:
    def __init__(self, settings: Settings, tone_mode: Optional[str] = None,
                 web_root: Optional[Path] = None, backend=None):
        self.settings = settings
        self.tone_mode = tone_mode
        self.web_root = Path(web_root) if web_root else _frozen_web_root()
        self._backend = backend
        self.events: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.running = False
        self.address: Optional[LanAddress] = None
        self.ring: Optional[PcmRing] = None
        self.engine = None
        self.server: Optional[RelayServer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._timer_raised = False

    # ------------------------------------------------------------------ #
    def _event(self, kind: str, msg: str) -> None:
        self.events.put((kind, msg))

    def url(self) -> Optional[str]:
        if not self.address:
            return None
        key = self.settings.key if self.settings.require_key else None
        return build_url(self.address.ip, self.settings.port, key)

    def alt_url(self) -> str:
        key = self.settings.key if self.settings.require_key else None
        return hostname_url(self.settings.port, key)

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self.running:
            return
        self.settings.clamp()
        self.address = pick_address(self.settings.address)
        if self.address is None:
            raise StartError("No network connection found. Connect the PC to Wi-Fi or Ethernet first.")
        configure_opus(self.settings.bitrate_kbps)

        self.ring = PcmRing()
        if self.tone_mode:
            self.engine = ToneCapture(self.ring, self.tone_mode, on_event=self._event)
        else:
            from .wasapi import WasapiBackend
            self.engine = CaptureManager(self.ring, self._backend or WasapiBackend(),
                                         device=self.settings.device, on_event=self._event)
        self.server = RelayServer(self.ring, self.engine, self.settings, self.web_root,
                                  advertise_ip=self.address.ip, on_event=self._event)

        self._loop = asyncio.new_event_loop()
        started = threading.Event()
        box: dict = {}

        def run_loop():
            asyncio.set_event_loop(self._loop)

            async def boot():
                try:
                    await self.server.start()
                except OSError as e:
                    box["error"] = e
                finally:
                    started.set()

            self._loop.create_task(boot())
            self._loop.run_forever()
            try:
                self._loop.close()
            except Exception:
                pass

        self._thread = threading.Thread(target=run_loop, name="relay-loop", daemon=True)
        self._thread.start()
        started.wait(10)
        if "error" in box:
            self._shutdown_loop()
            port = self.settings.port
            raise StartError(
                f"Could not listen on port {port}: {box['error']}. "
                f"Another program may be using it; pick a different port.")
        self._raise_timer_resolution()
        self.engine.start()
        self.running = True
        self._event("state", "running")

    def stop(self) -> None:
        if not self.running and self._loop is None:
            return
        try:
            if self.engine is not None:
                self.engine.stop()
            if self._loop is not None and self.server is not None and self._loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(self.server.stop(), self._loop)
                try:
                    fut.result(timeout=5)
                except Exception:
                    pass
        finally:
            self._shutdown_loop()
            if self.engine is not None and self.engine.is_alive():
                self.engine.join(timeout=3)
            self._restore_timer_resolution()
            self.running = False
            self._event("state", "stopped")

    def _shutdown_loop(self) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._loop = None
        self._thread = None

    def set_device(self, device: Optional[str]) -> None:
        self.settings.device = device
        if self.engine is not None:
            self.engine.set_device(device)

    def snapshot(self) -> dict:
        if self.server is None:
            return {}
        return self.server.snapshot()

    # Windows' default timer tick is ~15.6 ms, which would make 20 ms audio
    # pacing jittery. Ask for 1 ms for as long as we are running.
    def _raise_timer_resolution(self) -> None:
        if sys.platform == "win32" and not self._timer_raised:
            try:
                import ctypes
                ctypes.windll.winmm.timeBeginPeriod(1)
                self._timer_raised = True
            except Exception:
                pass

    def _restore_timer_resolution(self) -> None:
        if self._timer_raised:
            try:
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass
            self._timer_raised = False
