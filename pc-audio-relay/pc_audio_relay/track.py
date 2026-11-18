"""The WebRTC audio track that feeds Opus from the capture ring buffer."""
from __future__ import annotations

import asyncio
import time
from fractions import Fraction
from typing import Optional

import av
import numpy as np
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError

from .audio import PcmRing
from .config import CHANNELS, FRAME_SAMPLES, SAMPLE_RATE

_SILENCE = np.zeros((FRAME_SAMPLES, CHANNELS), dtype=np.int16)
# A short sound (UI click, gunshot) can be shorter than the prefill. If no new
# audio has arrived for this long, play what is there instead of waiting.
_FLUSH_AFTER_S = 0.03


class RelayAudioTrack(MediaStreamTrack):
    """Emits one 20 ms frame every 20 ms of wall-clock time.

    WASAPI loopback delivers *no data at all* while nothing is playing, so the
    clock cannot come from the capture device. We pace on the system clock and
    fill gaps with silence, which keeps the RTP stream continuous. Small drift
    between the audio clock and the system clock is absorbed by the ring:
    underruns are padded with silence, and if the ring grows past
    prefill + slack the oldest audio is dropped.
    """

    kind = "audio"

    def __init__(self, ring: PcmRing, prefill_ms: int = 40, slack_ms: int = 60):
        super().__init__()
        self._ring = ring
        self._prefill = SAMPLE_RATE * prefill_ms // 1000
        self._slack = SAMPLE_RATE * slack_ms // 1000
        self._playing = False
        self._start: Optional[float] = None
        self._pts = 0
        self.frames_sent = 0
        self.underruns = 0
        self.drift_drops = 0

    def _next_pcm(self) -> np.ndarray:
        ring = self._ring
        level = ring.level()
        if not self._playing:
            idle = time.monotonic() - ring.last_write
            if level >= self._prefill or (level > 0 and idle > _FLUSH_AFTER_S):
                self._playing = True
        if not self._playing:
            return _SILENCE
        if level > self._prefill + self._slack:
            ring.discard(level - self._prefill)
            self.drift_drops += 1
            level = ring.level()
        if level >= FRAME_SAMPLES:
            return ring.read(FRAME_SAMPLES)
        # Ran dry: send the tail padded with silence, then re-buffer.
        tail = ring.read(level)
        self._playing = False
        if level > 0 and time.monotonic() - ring.last_write < _FLUSH_AFTER_S:
            self.underruns += 1  # audio was still flowing, so this is a real underrun
        out = np.zeros((FRAME_SAMPLES, CHANNELS), dtype=np.int16)
        out[:level] = tail
        return out

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        now = time.monotonic()
        if self._start is None:
            self._start = now
        else:
            self._pts += FRAME_SAMPLES
            delay = self._start + self._pts / SAMPLE_RATE - now
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.5:  # event loop stalled badly; resync pacing, keep pts continuous
                self._start = now - self._pts / SAMPLE_RATE
        pcm = self._next_pcm()
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="stereo")
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, SAMPLE_RATE)
        self.frames_sent += 1
        return frame
