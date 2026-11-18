import asyncio
import time

import numpy as np

from pc_audio_relay.audio import PcmRing
from pc_audio_relay.config import FRAME_SAMPLES
from pc_audio_relay.track import RelayAudioTrack


def ramp(start, n):
    a = np.arange(start, start + n, dtype=np.int16)
    return np.stack([a, -a], axis=1)


def test_ring_wraparound_preserves_order():
    r = PcmRing(capacity_ms=10)  # 480 samples
    r.write(ramp(0, 300))
    assert np.array_equal(r.read(200), ramp(0, 200))
    r.write(ramp(300, 300))  # wraps
    out = r.read(400)
    assert np.array_equal(out, ramp(200, 400))
    assert r.level() == 0


def test_ring_overflow_drops_oldest_not_newest():
    r = PcmRing(capacity_ms=10)  # 480
    r.write(ramp(0, 400))
    r.write(ramp(400, 400))  # 320 over capacity
    assert r.level() == 480
    out = r.read(480)
    assert np.array_equal(out, ramp(320, 480))
    assert r.dropped_samples == 320


def test_ring_write_larger_than_capacity_keeps_tail():
    r = PcmRing(capacity_ms=10)
    r.write(ramp(0, 1000))
    assert np.array_equal(r.read(480), ramp(520, 480))


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


async def next_frame(track):
    f = await track.recv()
    return f.to_ndarray().reshape(-1, 2)


async def test_track_plays_silence_when_idle_and_keeps_clock():
    ring = PcmRing()
    t = RelayAudioTrack(ring, prefill_ms=40)
    t0 = time.monotonic()
    frames = [await next_frame(t) for _ in range(10)]
    elapsed = time.monotonic() - t0
    assert all((f == 0).all() for f in frames)
    assert 0.17 < elapsed < 0.30  # 9 intervals of 20 ms, paced on the clock
    assert t.frames_sent == 10


async def test_track_waits_for_prefill_then_plays_in_order():
    ring = PcmRing()
    t = RelayAudioTrack(ring, prefill_ms=40)  # 1920 samples
    ring.write(ramp(1, 960))  # below prefill, audio still "flowing"
    f = await next_frame(t)
    assert (f == 0).all(), "must not start before prefill is reached"
    ring.write(ramp(961, 1920))
    f = await next_frame(t)
    assert np.array_equal(f, ramp(1, 960)), "first real frame starts at the oldest sample"
    f = await next_frame(t)
    assert np.array_equal(f, ramp(961, 960))


async def test_short_sound_is_flushed_not_stuck():
    ring = PcmRing()
    t = RelayAudioTrack(ring, prefill_ms=40)
    ring.write(ramp(1, 480))  # 10 ms click, far below the 40 ms prefill
    await asyncio.sleep(0.06)  # no more audio arrives
    got = np.concatenate([await next_frame(t) for _ in range(2)])
    assert np.array_equal(got[:480], ramp(1, 480)), "short sound must still be played"
    assert (got[480:] == 0).all()


async def test_drift_drops_oldest_to_bound_latency():
    ring = PcmRing()
    t = RelayAudioTrack(ring, prefill_ms=40, slack_ms=60)
    ring.write(ramp(1, 48000 // 2))  # 500 ms queued: way over prefill+slack
    f = await next_frame(t)
    assert t.drift_drops == 1
    assert ring.level() <= (40 + 0) * 48 + 960  # back down near the prefill
    assert f.shape == (FRAME_SAMPLES, 2)
