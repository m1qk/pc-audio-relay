"""Audio plumbing: ring buffer, format conversion, capture manager, test tone.

Everything here is independent of Windows. The only Windows-specific piece is
the *backend* (see wasapi.py) that the capture manager is given, which lets the
device-change logic be tested with a fake backend.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Optional, Protocol

import numpy as np

from .config import CHANNELS, SAMPLE_RATE

EventFn = Callable[[str, str], None]  # (kind, message)


# --------------------------------------------------------------------------- #
# Ring buffer
# --------------------------------------------------------------------------- #
class PcmRing:
    """Thread-safe ring of 48 kHz stereo int16 frames.

    The capture callback writes into it; the WebRTC track reads 20 ms at a time.
    If the writer gets too far ahead the *oldest* audio is dropped, so latency
    can never grow without bound.
    """

    def __init__(self, capacity_ms: int = 1000):
        self._cap = SAMPLE_RATE * capacity_ms // 1000
        self._buf = np.zeros((self._cap, CHANNELS), dtype=np.int16)
        self._r = 0
        self._n = 0
        self._lock = threading.Lock()
        self.dropped_samples = 0
        self.last_write = 0.0  # time.monotonic() of the most recent write

    def level(self) -> int:
        with self._lock:
            return self._n

    def clear(self) -> None:
        with self._lock:
            self._r = 0
            self._n = 0

    def write(self, pcm: np.ndarray) -> None:
        n = len(pcm)
        if n == 0:
            return
        with self._lock:
            if n >= self._cap:
                self.dropped_samples += self._n + (n - self._cap)
                pcm = pcm[-self._cap:]
                n = self._cap
                self._r = 0
                self._n = 0
            overflow = self._n + n - self._cap
            if overflow > 0:
                self._r = (self._r + overflow) % self._cap
                self._n -= overflow
                self.dropped_samples += overflow
            w = (self._r + self._n) % self._cap
            first = min(n, self._cap - w)
            self._buf[w:w + first] = pcm[:first]
            if first < n:
                self._buf[: n - first] = pcm[first:]
            self._n += n
            self.last_write = time.monotonic()

    def read(self, n: int) -> np.ndarray:
        """Read up to n frames (fewer if the ring holds fewer)."""
        with self._lock:
            n = min(n, self._n)
            out = np.empty((n, CHANNELS), dtype=np.int16)
            first = min(n, self._cap - self._r)
            out[:first] = self._buf[self._r:self._r + first]
            if first < n:
                out[first:] = self._buf[: n - first]
            self._r = (self._r + n) % self._cap
            self._n -= n
            return out

    def discard(self, n: int) -> int:
        """Drop the oldest n frames; returns how many were dropped."""
        with self._lock:
            n = min(n, self._n)
            self._r = (self._r + n) % self._cap
            self._n -= n
            self.dropped_samples += n
            return n


# --------------------------------------------------------------------------- #
# Format conversion: device PCM -> 48 kHz stereo int16
# --------------------------------------------------------------------------- #
_LAYOUTS = {1: "mono", 2: "stereo", 3: "2.1", 4: "quad", 5: "5.0", 6: "5.1", 7: "6.1", 8: "7.1"}


class StereoConverter:
    """Converts interleaved device PCM to 48 kHz stereo int16 using PyAV's
    libswresample (proper surround downmix and high quality resampling)."""

    def __init__(self, in_rate: int, in_channels: int, sample_format: str = "int16"):
        import av  # local import keeps module importable without PyAV

        self._av = av
        self.in_rate = int(in_rate)
        self.in_channels = int(in_channels)
        self.sample_format = sample_format
        self._layout = _LAYOUTS.get(self.in_channels)
        self._pts = 0
        self._resampler = av.AudioResampler(format="s16", layout="stereo", rate=SAMPLE_RATE)

    def process(self, data: bytes) -> np.ndarray:
        ch = self.in_channels
        if self.sample_format == "float32":
            f = np.frombuffer(data, dtype=np.float32)
            arr = np.clip(f * 32767.0, -32768, 32767).astype(np.int16)
        else:
            arr = np.frombuffer(data, dtype=np.int16)
        n = len(arr) // ch
        if n == 0:
            return np.empty((0, CHANNELS), dtype=np.int16)
        arr = arr[: n * ch]
        layout = self._layout
        if layout is None:  # unusual channel count: keep the first two channels
            arr = arr.reshape(n, ch)[:, :2].copy()
            ch, layout = 2, "stereo"
        frame = self._av.AudioFrame.from_ndarray(arr.reshape(1, -1), format="s16", layout=layout)
        frame.sample_rate = self.in_rate
        frame.pts = self._pts
        frame.time_base = Fraction(1, self.in_rate)
        self._pts += n
        chunks = [f.to_ndarray().reshape(-1, CHANNELS) for f in self._resampler.resample(frame)]
        if not chunks:
            return np.empty((0, CHANNELS), dtype=np.int16)
        return chunks[0] if len(chunks) == 1 else np.concatenate(chunks)


# --------------------------------------------------------------------------- #
# Backend interface (implemented for real in wasapi.py)
# --------------------------------------------------------------------------- #
@dataclass
class DeviceInfo:
    name: str
    channels: int = 2
    rate: int = 48000


@dataclass
class ProbeResult:
    devices: list[DeviceInfo] = field(default_factory=list)
    default_name: Optional[str] = None


class CaptureStream(Protocol):
    rate: int
    channels: int
    sample_format: str

    def is_active(self) -> bool: ...
    def close(self) -> None: ...


class AudioBackend(Protocol):
    def probe(self) -> ProbeResult:
        """Enumerate playback devices with a *fresh* view of the system."""

    def open(self, device_name: str, on_pcm: Callable[[bytes, int, int, str], None]) -> CaptureStream:
        """Start loopback capture of the named playback device.

        on_pcm(data, rate, channels, sample_format) is called from the audio
        thread with interleaved PCM; sample_format is "int16" or "float32".
        """


# --------------------------------------------------------------------------- #
# Capture manager
# --------------------------------------------------------------------------- #
class CaptureManager(threading.Thread):
    """Keeps a loopback capture running on the right device.

    * device=None  -> follow the Windows default playback device; when the
      default changes (headphones plugged in, Bluetooth connects, ...) capture
      moves to the new device automatically.
    * device="X"   -> capture X; if it disappears, wait for it and resume.

    States: stopped, starting, running, waiting, error.
    """

    POLL_SECONDS = 3.0

    def __init__(self, ring: PcmRing, backend: AudioBackend, device: Optional[str] = None,
                 on_event: Optional[EventFn] = None, poll_seconds: Optional[float] = None):
        super().__init__(name="capture-manager", daemon=True)
        self.ring = ring
        self.backend = backend
        self._device = device
        self._on_event = on_event or (lambda kind, msg: None)
        self._poll = poll_seconds if poll_seconds is not None else self.POLL_SECONDS
        self._stop_evt = threading.Event()
        self._wake = threading.Event()
        self.state = "starting"
        self.state_detail = ""
        self.device_name: Optional[str] = None  # device currently being captured
        self.level = 0.0  # 0..1 peak of the latest chunk
        self.restarts = 0
        self._converter: Optional[StereoConverter] = None

    # -- public API -------------------------------------------------------- #
    def set_device(self, device: Optional[str]) -> None:
        self._device = device
        self._wake.set()

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake.set()

    # -- internals --------------------------------------------------------- #
    def _emit(self, kind: str, msg: str) -> None:
        try:
            self._on_event(kind, msg)
        except Exception:
            pass

    def _set_state(self, state: str, detail: str = "") -> None:
        if state != self.state or detail != self.state_detail:
            self.state, self.state_detail = state, detail
            self._emit("capture", f"{state}: {detail}" if detail else state)

    def _on_pcm(self, data: bytes, rate: int, channels: int, fmt: str) -> None:
        conv = self._converter
        try:
            if conv is None or (conv.in_rate, conv.in_channels, conv.sample_format) != (rate, channels, fmt):
                conv = self._converter = StereoConverter(rate, channels, fmt)
            pcm = conv.process(data)
        except Exception as e:  # never let an exception escape into PortAudio
            self._emit("log", f"audio conversion error: {e}")
            return
        if len(pcm):
            self.level = float(np.abs(pcm).max()) / 32768.0
            self.ring.write(pcm)

    def _resolve(self, probe: ProbeResult) -> Optional[str]:
        names = [d.name for d in probe.devices]
        if self._device is None:
            return probe.default_name if probe.default_name in names else None
        return self._device if self._device in names else None

    def run(self) -> None:
        stream: Optional[CaptureStream] = None
        current: Optional[str] = None
        wanted_at_open: Optional[str] = None
        try:
            while not self._stop_evt.is_set():
                if stream is None:
                    self._set_state("starting", "looking for playback device")
                    try:
                        probe = self.backend.probe()
                    except Exception as e:
                        self._set_state("error", f"could not list audio devices: {e}")
                        self._wait(3.0)
                        continue
                    target = self._resolve(probe)
                    if target is None:
                        what = "no playback device found" if self._device is None else f"device not found: {self._device}"
                        self._set_state("waiting", what)
                        self._wait(self._poll)
                        continue
                    try:
                        stream = self.backend.open(target, self._on_pcm)
                    except Exception as e:
                        stream = None
                        self._set_state("error", f"could not open '{target}': {e}")
                        self._wait(3.0)
                        continue
                    current, wanted_at_open = target, self._device
                    self.device_name = target
                    self.restarts += 1
                    self._set_state("running", target)
                    continue

                # Stream is open: wait, then verify it is still the right one.
                self._wait(self._poll)
                if self._stop_evt.is_set():
                    break
                reason = None
                if self._device != wanted_at_open:
                    reason = "device selection changed"
                elif not stream.is_active():
                    reason = "stream stopped (device removed or format changed)"
                else:
                    try:
                        probe = self.backend.probe()
                    except Exception:
                        probe = None
                    if probe is not None:
                        target = self._resolve(probe)
                        if target is None:
                            reason = "device removed"
                        elif target != current:
                            reason = f"default device changed to '{target}'"
                if reason:
                    self._emit("log", f"restarting capture: {reason}")
                    self._close(stream)
                    stream = None
                    self.device_name = None
                    self.ring.clear()
        finally:
            if stream is not None:
                self._close(stream)
            self.device_name = None
            self._set_state("stopped", "")

    def _close(self, stream: CaptureStream) -> None:
        self._converter = None
        try:
            stream.close()
        except Exception as e:
            self._emit("log", f"error closing stream: {e}")

    def _wait(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()


# --------------------------------------------------------------------------- #
# Test tone (used by --tone and by the automated tests)
# --------------------------------------------------------------------------- #
class ToneCapture(threading.Thread):
    """Feeds the ring with a real-time test signal instead of capturing audio.

    mode="tone":  440 Hz in the left channel, 880 Hz in the right (verifies
                  stereo end to end).
    mode="click": a short click once per second over a very quiet continuous tone (delay measurements).
    mode="noise": white noise (worst case for the Opus bitrate).
    """

    def __init__(self, ring: PcmRing, mode: str = "tone", on_event: Optional[EventFn] = None):
        super().__init__(name="tone-source", daemon=True)
        self.ring = ring
        self.mode = mode
        self._stop_evt = threading.Event()
        self.state = "running"
        self.state_detail = f"test {mode}"
        self.device_name = f"Test {mode}"
        self.level = 0.0
        self.restarts = 1
        self.t0_wall = 0.0
        self._on_event = on_event

    def set_device(self, device) -> None:  # API parity with CaptureManager
        pass

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        chunk = SAMPLE_RATE // 100  # 10 ms
        t0 = time.monotonic()
        self.t0_wall = time.time()  # lets tools/loopback_latency_probe.py timestamp each click
        produced = 0
        amp = 0.3
        while not self._stop_evt.is_set():
            idx = np.arange(produced, produced + chunk, dtype=np.float64)
            if self.mode == "noise":
                rng = np.random.default_rng(produced)
                left = rng.uniform(-amp, amp, chunk)
                right = rng.uniform(-amp, amp, chunk)
            elif self.mode == "click":
                pos = (idx % SAMPLE_RATE)
                env = (pos < 240).astype(np.float64)  # 5 ms burst once per second
                wave = np.sin(2 * math.pi * 1000 * idx / SAMPLE_RATE) * env
                # A quiet continuous bed keeps audio flowing like real music or games. Without it an
                # isolated click is held back by the sender's short-sound flush and reads ~30 ms late.
                bed = np.sin(2 * math.pi * 220 * idx / SAMPLE_RATE) * 0.006
                left = right = wave * 0.8 + bed
            else:
                left = np.sin(2 * math.pi * 440 * idx / SAMPLE_RATE) * amp
                right = np.sin(2 * math.pi * 880 * idx / SAMPLE_RATE) * amp
            pcm = (np.stack([left, right], axis=1) * 32767).astype(np.int16)
            self.level = float(np.abs(pcm).max()) / 32768.0
            self.ring.write(pcm)
            produced += chunk
            due = t0 + produced / SAMPLE_RATE
            delay = due - time.monotonic()
            if delay > 0:
                self._stop_evt.wait(delay)
        self.state = "stopped"
