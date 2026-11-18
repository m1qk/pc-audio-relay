#!/usr/bin/env python3
"""Write a WAV with one click per second, for measuring end-to-end delay.

Play it on the PC (any player, default output device). The sender streams it to
the iPhone while the PC speakers play it directly; record both with a mic or a
line-in and run measure_delay.py on the recording.

A very quiet continuous tone is mixed in by default. Without it the audio stream
is silent between clicks, and an isolated click is held back ~30 ms by the
sender's short-sound flush, so you would measure slightly MORE delay than music
or games (which flow continuously) actually have.
"""
import argparse
import wave

import numpy as np

RATE = 48000


def build(seconds: int, bed_db: float | None) -> np.ndarray:
    n = seconds * RATE
    t = np.arange(n) / RATE
    x = np.zeros(n)
    burst = int(0.005 * RATE)
    env = np.ones(burst)
    env[-48:] *= np.linspace(1, 0, 48)
    tone = np.sin(2 * np.pi * 2000 * np.arange(burst) / RATE) * env
    for k in range(1, seconds - 1):
        i = k * RATE
        x[i:i + burst] += tone * 0.9
    if bed_db is not None:
        x += np.sin(2 * np.pi * 220 * t) * (10 ** (bed_db / 20))
    return np.clip(x, -1, 1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--output", default="click_track.wav")
    ap.add_argument("-s", "--seconds", type=int, default=40)
    ap.add_argument("--bed-db", type=float, default=-45.0, help="level of the quiet continuous tone; use 'none' via --no-bed")
    ap.add_argument("--no-bed", action="store_true")
    a = ap.parse_args()
    x = build(a.seconds, None if a.no_bed else a.bed_db)
    pcm = (x * 32767).astype("<i2")
    with wave.open(a.output, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.stack([pcm, pcm], 1).tobytes())
    print(f"wrote {a.output}: {a.seconds} s, one click per second" + ("" if a.no_bed else f", bed {a.bed_db} dBFS"))


if __name__ == "__main__":
    main()
