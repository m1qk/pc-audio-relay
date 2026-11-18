"""Opus encoder tuning for aiortc.

aiortc hard-codes `application=voip` at 96 kbps, which is tuned for speech.
For games and music we want `application=audio` and a higher bitrate. aiortc
has no public hook for this, so we wrap OpusEncoder.__init__.
"""
from __future__ import annotations

_state = {"bitrate": 128_000, "patched": False}


def configure_opus(bitrate_kbps: int = 128) -> None:
    from aiortc.codecs import opus

    _state["bitrate"] = int(bitrate_kbps) * 1000
    if _state["patched"]:
        return
    original_init = opus.OpusEncoder.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        # The codec context is opened lazily on first encode, so setting these
        # here (before any frame is encoded) takes effect.
        self.codec.bit_rate = _state["bitrate"]
        self.codec.options = {"application": "audio", "vbr": "on"}

    opus.OpusEncoder.__init__ = patched_init
    _state["patched"] = True
