#!/usr/bin/env python3
"""Measure end-to-end audio delay from a recording of click_track.wav.

Two ways to record (see docs/LATENCY.md):

  --mode mic     ONE microphone hears the PC speakers (direct sound) and the
                 iPhone speaker (relayed sound). Each second shows two clicks;
                 the gap between them is the delay.
  --mode lines   A two-channel recorder: channel 1 = PC line out, channel 2 =
                 iPhone headphone output. The offset between channels is the delay.

Reads 16/24/32-bit PCM and 32-bit float WAV files (what Audacity exports).
"""
from __future__ import annotations

import argparse
import statistics
import struct
import sys

import numpy as np

SOUND_MPS = 343.0


def read_wav(path: str) -> tuple[np.ndarray, int]:
    """Returns (samples shaped (n, channels) as float in -1..1, sample rate)."""
    data = open(path, "rb").read()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    pos, fmt, pcm = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            tag, ch, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
            if tag == 0xFFFE and len(body) >= 26:
                tag = struct.unpack("<H", body[24:26])[0]
            fmt = (tag, ch, rate, bits)
        elif cid == b"data":
            pcm = body
        pos += 8 + size + (size & 1)
    if fmt is None or pcm is None:
        raise ValueError("WAV file has no fmt or data chunk")
    tag, ch, rate, bits = fmt
    if tag == 3 and bits == 32:
        x = np.frombuffer(pcm[: len(pcm) // 4 * 4], dtype="<f4").astype(np.float64)
    elif tag == 1 and bits == 16:
        x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2") / 32768.0
    elif tag == 1 and bits == 24:
        b = np.frombuffer(pcm[: len(pcm) // 3 * 3], dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        x = np.where(v >= 1 << 23, v - (1 << 24), v) / float(1 << 23)
    elif tag == 1 and bits == 32:
        x = np.frombuffer(pcm[: len(pcm) // 4 * 4], dtype="<i4") / 2147483648.0
    else:
        raise ValueError(f"unsupported WAV format (tag {tag}, {bits} bit)")
    n = len(x) // ch
    return x[: n * ch].reshape(n, ch), rate


def highpass(x: np.ndarray, rate: int, cutoff: float = 800.0) -> np.ndarray:
    """Removes the quiet low-frequency bed and room rumble so clicks stand out
    (subtracts a moving average about one cutoff-period wide; vectorized)."""
    n = max(3, int(rate / cutoff))
    c = np.cumsum(np.r_[0.0, x])
    avg = np.empty_like(x)
    lo = np.clip(np.arange(len(x)) - n // 2, 0, len(x))
    hi = np.clip(np.arange(len(x)) + n // 2 + 1, 0, len(x))
    avg[:] = (c[hi] - c[lo]) / np.maximum(hi - lo, 1)
    return x - avg


def onsets(x: np.ndarray, rate: int, refractory_ms: float = 12.0, rel_threshold: float = 0.25) -> np.ndarray:
    """Sample index of the start of each click."""
    env = np.abs(highpass(x, rate))
    w = max(1, int(0.001 * rate))  # 1 ms moving maximum bridges the zero crossings of the click tone
    pad = np.pad(env, (0, w - 1))
    env = np.max(np.lib.stride_tricks.sliding_window_view(pad, w), axis=1)
    peak = np.percentile(env, 99.95)
    if peak <= 0:
        return np.array([], dtype=int)
    above = env > rel_threshold * peak
    idx = np.flatnonzero(above & ~np.r_[False, above[:-1]])
    keep, last = [], -10 ** 9
    gap = int(refractory_ms * rate / 1000)
    for i in idx:
        if i - last >= gap:
            keep.append(i)
            last = i
    return np.array(keep, dtype=int)


def delays_two_channels(x: np.ndarray, rate: int) -> list[float]:
    a, b = onsets(x[:, 0], rate), onsets(x[:, 1], rate)
    out = []
    for t in a:
        j = np.argmin(np.abs(b - t)) if len(b) else None
        if j is not None and abs(b[j] - t) < 0.9 * rate / 2:
            out.append((b[j] - t) * 1000.0 / rate)
    return out


def delays_one_mic(x: np.ndarray, rate: int, period_s: float = 1.0) -> list[float]:
    """Each period has the direct click, then the relayed one."""
    o = onsets(x[:, 0], rate)
    if len(o) < 2:
        return []
    period = int(period_s * rate)
    base = o[0]
    out = []
    groups: dict[int, list[int]] = {}
    for t in o:
        groups.setdefault(int((t - base + period * 0.1) // period), []).append(t)
    for g in groups.values():
        if len(g) == 2:
            out.append((g[1] - g[0]) * 1000.0 / rate)
    return out


def summarize(values: list[float]) -> str:
    if not values:
        return "No click pairs found. Check the recording level and that both sources were captured."
    med = statistics.median(values)
    return (f"clicks measured: {len(values)}\n"
            f"delay (median): {med:.1f} ms\n"
            f"mean {statistics.fmean(values):.1f} ms   min {min(values):.1f} ms   max {max(values):.1f} ms   "
            f"spread (stdev) {statistics.pstdev(values):.1f} ms")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording")
    ap.add_argument("--mode", choices=["mic", "lines"], required=True)
    ap.add_argument("--phone-farther-by-m", type=float, default=0.0,
                    help="mic mode: how much farther the iPhone speaker is from the mic than the PC speakers (metres). "
                         "Sound needs ~2.9 ms per metre, which is subtracted from the result.")
    a = ap.parse_args()
    x, rate = read_wav(a.recording)
    if a.mode == "lines":
        if x.shape[1] < 2:
            print("lines mode needs a 2-channel recording", file=sys.stderr)
            return 2
        vals = delays_two_channels(x, rate)
    else:
        x = x.mean(axis=1, keepdims=True)
        vals = delays_one_mic(x, rate)
        vals = [v - a.phone_farther_by_m / SOUND_MPS * 1000.0 for v in vals]
    print(summarize(vals))
    return 0 if vals else 1


if __name__ == "__main__":
    raise SystemExit(main())
