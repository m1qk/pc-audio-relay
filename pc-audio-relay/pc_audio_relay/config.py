"""Shared constants and persisted settings."""
from __future__ import annotations

import json
import os
import secrets
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

APP_NAME = "PC Audio Relay"
VERSION = "0.1.0"
PROTOCOL_VERSION = 1

SAMPLE_RATE = 48_000
CHANNELS = 2
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 960 samples = one Opus frame

DEFAULT_PORT = 8765
FIREWALL_RULE_NAME = "PC Audio Relay"


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "PCAudioRelay"


@dataclass
class Settings:
    port: int = DEFAULT_PORT
    # None means "follow whatever Windows uses as the default playback device".
    device: str | None = None
    # None means "pick the LAN address automatically".
    address: str | None = None
    # Sender-side jitter absorber. Lower = less delay, higher = fewer dropouts.
    capture_buffer_ms: int = 40
    bitrate_kbps: int = 128
    # The pairing key is part of the QR URL; it keeps other people on the same
    # Wi-Fi from listening in.
    require_key: bool = True
    key: str = ""

    def clamp(self) -> "Settings":
        self.port = min(max(int(self.port), 1024), 65535)
        self.capture_buffer_ms = min(max(int(self.capture_buffer_ms), 10), 300)
        self.bitrate_kbps = min(max(int(self.bitrate_kbps), 32), 320)
        if not self.key:
            self.key = secrets.token_urlsafe(6)
        return self

    @classmethod
    def path(cls) -> Path:
        return config_dir() / "settings.json"

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or cls.path()
        known = {f.name for f in fields(cls)}
        data: dict = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = {k: v for k, v in raw.items() if k in known}
        except (OSError, ValueError):
            pass
        s = cls(**data).clamp()
        return s

    def save(self, path: Path | None = None) -> None:
        path = path or self.path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            # Settings are a convenience; never crash because they can't be saved.
            pass
