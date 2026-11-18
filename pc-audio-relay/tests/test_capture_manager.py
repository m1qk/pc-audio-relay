import threading
import time

import numpy as np

from pc_audio_relay.audio import (CaptureManager, DeviceInfo, PcmRing, ProbeResult,
                                  StereoConverter)


class FakeStream:
    def __init__(self, backend, name, on_pcm, rate=48000, channels=2):
        self.backend, self.name, self.on_pcm = backend, name, on_pcm
        self.rate, self.channels, self.sample_format = rate, channels, "int16"
        self.active, self.closed = True, False

    def is_active(self):
        return self.active

    def close(self):
        self.closed = True
        self.active = False

    def feed(self, data: bytes):
        self.on_pcm(data, self.rate, self.channels, self.sample_format)


class FakeBackend:
    def __init__(self, devices, default):
        self.devices, self.default = list(devices), default
        self.opened: list[FakeStream] = []
        self.lock = threading.Lock()

    def probe(self):
        with self.lock:
            return ProbeResult([DeviceInfo(n) for n in self.devices], self.default)

    def open(self, name, on_pcm):
        with self.lock:
            if name not in self.devices:
                raise RuntimeError("not available")
            s = FakeStream(self, name, on_pcm)
            self.opened.append(s)
            return s


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def make(backend, device=None):
    events = []
    m = CaptureManager(PcmRing(), backend, device=device, poll_seconds=0.05,
                       on_event=lambda k, msg: events.append((k, msg)))
    m.start()
    return m, events


def test_follows_default_device_when_it_changes():
    b = FakeBackend(["Speakers", "Headphones"], default="Speakers")
    m, ev = make(b)
    try:
        assert wait_for(lambda: m.state == "running" and m.device_name == "Speakers")
        b.default = "Headphones"  # user plugs in headphones; Windows switches default
        assert wait_for(lambda: m.device_name == "Headphones"), ev
        assert b.opened[0].closed, "old stream must be closed"
        assert m.restarts == 2
    finally:
        m.stop(); m.join(2)
    assert m.state == "stopped"


def test_fixed_device_waits_when_unplugged_and_resumes():
    b = FakeBackend(["Speakers", "USB DAC"], default="Speakers")
    m, ev = make(b, device="USB DAC")
    try:
        assert wait_for(lambda: m.device_name == "USB DAC")
        b.devices.remove("USB DAC")  # unplugged
        assert wait_for(lambda: m.state == "waiting"), (m.state, ev)
        assert m.device_name is None
        b.devices.append("USB DAC")  # plugged back in
        assert wait_for(lambda: m.state == "running" and m.device_name == "USB DAC")
    finally:
        m.stop(); m.join(2)


def test_user_switching_device_restarts_capture():
    b = FakeBackend(["Speakers", "USB DAC"], default="Speakers")
    m, _ = make(b)
    try:
        assert wait_for(lambda: m.device_name == "Speakers")
        m.set_device("USB DAC")
        assert wait_for(lambda: m.device_name == "USB DAC")
        m.set_device(None)  # back to following Windows' default
        assert wait_for(lambda: m.device_name == "Speakers")
    finally:
        m.stop(); m.join(2)


def test_dead_stream_is_reopened():
    b = FakeBackend(["Speakers"], default="Speakers")
    m, _ = make(b)
    try:
        assert wait_for(lambda: m.state == "running")
        b.opened[0].active = False  # e.g. sample-rate change invalidated the stream
        assert wait_for(lambda: len(b.opened) == 2 and m.state == "running")
    finally:
        m.stop(); m.join(2)


def test_open_failure_is_reported_and_retried():
    class Flaky(FakeBackend):
        fails = 1
        def open(self, name, on_pcm):
            if self.fails:
                self.fails -= 1
                raise RuntimeError("device busy")
            return super().open(name, on_pcm)
    b = Flaky(["Speakers"], default="Speakers")
    m, ev = make(b)
    try:
        assert wait_for(lambda: any("device busy" in msg for _, msg in ev))
        assert wait_for(lambda: m.state == "running", timeout=6)
    finally:
        m.stop(); m.join(2)


def test_pcm_reaches_ring_and_sets_level():
    b = FakeBackend(["Speakers"], default="Speakers")
    m, _ = make(b)
    try:
        assert wait_for(lambda: m.state == "running")
        tone = (np.sin(np.arange(480) * 0.1) * 16000).astype(np.int16)
        b.opened[0].feed(np.stack([tone, tone], 1).tobytes())
        assert m.ring.level() == 480
        assert 0.4 < m.level < 0.5
    finally:
        m.stop(); m.join(2)


# --- format conversion ----------------------------------------------------- #
def sine(freq, rate, n, amp=12000):
    return (np.sin(2 * np.pi * freq * np.arange(n) / rate) * amp).astype(np.int16)


def dominant(x, rate=48000):
    spec = np.abs(np.fft.rfft(x.astype(float) * np.hanning(len(x))))
    return np.fft.rfftfreq(len(x), 1 / rate)[spec.argmax()]


def test_converter_resamples_44k1_stereo_to_48k_keeping_channels_separate():
    conv = StereoConverter(44100, 2)
    n = 44100  # one second
    l, r = sine(440, 44100, n), sine(1000, 44100, n)
    out = np.concatenate([conv.process(np.stack([l, r], 1)[i:i + 441].tobytes()) for i in range(0, n, 441)])
    assert abs(len(out) - 48000) < 200
    assert abs(dominant(out[:, 0]) - 440) < 5 and abs(dominant(out[:, 1]) - 1000) < 5


def test_converter_downmixes_5_1_to_stereo():
    conv = StereoConverter(48000, 6)
    n = 48000
    fl, fr = sine(500, 48000, n), np.zeros(n, np.int16)
    silent = np.zeros(n, np.int16)
    six = np.stack([fl, fr, silent, silent, silent, silent], 1)  # FL FR FC LFE BL BR
    out = np.concatenate([conv.process(six[i:i + 480].tobytes()) for i in range(0, n, 480)])
    assert abs(len(out) - n) < 200
    assert np.abs(out[:, 0]).max() > 3000, "front-left must reach the left output"
    assert np.abs(out[:, 1]).max() < 500, "nothing was playing on front-right"


def test_converter_unusual_channel_count_keeps_first_two():
    conv = StereoConverter(48000, 3 + 9)  # 12 channels: no layout; falls back
    n = 4800
    data = np.zeros((n, 12), np.int16)
    data[:, 0] = sine(700, 48000, n)
    out = conv.process(data.tobytes())
    assert len(out) > 0 and np.abs(out[:, 0]).max() > 3000


def test_converter_float32_input():
    conv = StereoConverter(48000, 2, "float32")
    f = (np.sin(np.arange(960) * 0.05) * 0.5).astype(np.float32)
    out = conv.process(np.stack([f, f], 1).tobytes())
    assert 14000 < np.abs(out).max() < 17000
