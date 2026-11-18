"""The delay analyzer must recover known delays from synthetic recordings."""
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import make_click_track as mct  # noqa: E402
import measure_delay as md  # noqa: E402

RATE = 48000


def write_wav(path, x, width=2):
    """x: (n, ch) float in -1..1."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(x.shape[1]); w.setsampwidth(width); w.setframerate(RATE)
        if width == 2:
            w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
        else:  # 24-bit
            v = (np.clip(x, -1, 1) * (2 ** 23 - 1)).astype(np.int32).reshape(-1)
            b = np.stack([v & 255, (v >> 8) & 255, (v >> 16) & 255], 1).astype(np.uint8)
            w.writeframes(b.tobytes())


def clicks_only(seconds=12):
    return mct.build(seconds, None)


def shifted(x, ms):
    s = int(round(ms * RATE / 1000))
    return np.r_[np.zeros(s), x][: len(x)]


@pytest.mark.parametrize("delay_ms", [38.0, 85.5, 240.0])
def test_one_mic_recovers_known_delay_with_noise_and_bed(tmp_path, delay_ms):
    rng = np.random.default_rng(1)
    src = mct.build(14, -45.0)                       # what the PC plays (with the quiet bed)
    direct = 0.6 * src
    relayed = 0.4 * shifted(src, delay_ms)           # iPhone speaker, quieter, later
    rec = direct + relayed + rng.normal(0, 0.004, len(src))
    p = tmp_path / "mic.wav"
    write_wav(p, rec[:, None])
    x, rate = md.read_wav(str(p))
    vals = md.delays_one_mic(x, rate)
    assert len(vals) >= 9
    assert abs(np.median(vals) - delay_ms) < 1.5, (np.median(vals), delay_ms)


def test_two_channel_mode_and_24bit_wav(tmp_path):
    src = clicks_only(12)
    rec = np.stack([src, shifted(src, 112.0)], 1)
    p = tmp_path / "lines.wav"
    write_wav(p, rec, width=3)
    x, rate = md.read_wav(str(p))
    vals = md.delays_two_channels(x, rate)
    assert len(vals) >= 9 and abs(np.median(vals) - 112.0) < 1.0


def test_acoustic_distance_correction_via_cli(tmp_path):
    rec = clicks_only(12) * 0.6 + 0.4 * shifted(clicks_only(12), 60.0)
    p = tmp_path / "mic.wav"
    write_wav(p, rec[:, None])
    out = subprocess.run([sys.executable, str(Path(md.__file__)), str(p), "--mode", "mic",
                          "--phone-farther-by-m", "1.0"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    median = float(next(l for l in out.stdout.splitlines() if l.startswith("delay (median)")).split()[2])
    assert abs(median - (60.0 - 1000 / 343)) < 1.5   # 1 m of extra air = 2.9 ms


def test_silence_reports_no_clicks(tmp_path):
    p = tmp_path / "silence.wav"
    write_wav(p, np.zeros((RATE * 3, 1)))
    out = subprocess.run([sys.executable, str(Path(md.__file__)), str(p), "--mode", "mic"], capture_output=True, text=True)
    assert out.returncode == 1 and "No click pairs" in out.stdout


def test_click_track_generator_writes_expected_file(tmp_path):
    p = tmp_path / "t.wav"
    subprocess.run([sys.executable, str(Path(mct.__file__)), "-o", str(p), "-s", "10"], check=True, capture_output=True)
    x, rate = md.read_wav(str(p))
    assert rate == 48000 and x.shape == (480000, 2)
    assert len(md.onsets(x[:, 0], rate)) == 8   # clicks at seconds 1..8 (first/last second left clean)
