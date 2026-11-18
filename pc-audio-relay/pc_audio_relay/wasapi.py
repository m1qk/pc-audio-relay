"""Windows WASAPI loopback backend (via PyAudioWPatch).

IMPORTANT DESIGN NOTE
PortAudio enumerates devices once, when it is initialised, and never notices
later changes (headphones plugged in, Bluetooth connected, default device
switched). To get a *fresh* view we run the enumeration in a short-lived
subprocess (`--probe`), which has its own PortAudio state. Opening a stream
re-initialises PortAudio in this process, so a restart also sees new devices.

This module can only be exercised on Windows. The rest of the app is tested
with a fake backend.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .audio import DeviceInfo, ProbeResult

LOOPBACK_SUFFIX = " [Loopback]"


def _base_name(name: str) -> str:
    return name[: -len(LOOPBACK_SUFFIX)] if name.endswith(LOOPBACK_SUFFIX) else name


def probe_in_process() -> ProbeResult:
    """Enumerate loopback devices. Call only from a fresh process (see above)."""
    import pyaudiowpatch as pyaudio

    pa = pyaudio.PyAudio()
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_index = wasapi.get("defaultOutputDevice", -1)
        default_name = None
        if default_index is not None and default_index >= 0:
            default_name = _base_name(pa.get_device_info_by_index(default_index)["name"])
        devices = []
        for d in pa.get_loopback_device_info_generator():
            devices.append(DeviceInfo(
                name=_base_name(d["name"]),
                channels=int(d["maxInputChannels"]),
                rate=int(d["defaultSampleRate"]),
            ))
        return ProbeResult(devices=devices, default_name=default_name)
    finally:
        pa.terminate()


def _probe_command() -> tuple[list[str], str | None]:
    """Command + working directory for the probe subprocess. (No `-I`: isolated
    mode would stop `-m pc_audio_relay` from finding the package when run from
    source.)"""
    if getattr(sys, "frozen", False):  # PyInstaller build
        return [sys.executable, "--probe"], None
    root = str(Path(__file__).resolve().parent.parent)
    return [sys.executable, "-m", "pc_audio_relay", "--probe"], root


class _WasapiStream:
    def __init__(self, pa, stream, rate: int, channels: int, sample_format: str):
        self._pa, self._stream = pa, stream
        self.rate, self.channels, self.sample_format = rate, channels, sample_format

    def is_active(self) -> bool:
        try:
            return bool(self._stream.is_active())
        except Exception:
            return False

    def close(self) -> None:
        try:
            try:
                self._stream.stop_stream()
            finally:
                self._stream.close()
        finally:
            self._pa.terminate()


class WasapiBackend:
    """AudioBackend implementation for Windows."""

    def probe(self) -> ProbeResult:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        cmd, cwd = _probe_command()
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=15, **kwargs)
        if out.returncode != 0:
            raise RuntimeError((out.stderr or out.stdout or "probe failed").strip().splitlines()[-1])
        data = json.loads(out.stdout.strip().splitlines()[-1])
        if "error" in data:
            raise RuntimeError(data["error"])
        return ProbeResult(
            devices=[DeviceInfo(**d) for d in data["devices"]],
            default_name=data["default_name"],
        )

    def open(self, device_name: str, on_pcm: Callable[[bytes, int, int, str], None]) -> _WasapiStream:
        import pyaudiowpatch as pyaudio

        pa = pyaudio.PyAudio()  # fresh init => fresh device list
        try:
            info = None
            for d in pa.get_loopback_device_info_generator():
                if _base_name(d["name"]) == device_name:
                    info = d
                    break
            if info is None:
                raise RuntimeError(f"device '{device_name}' is not available")
            rate = int(info["defaultSampleRate"])
            channels = int(info["maxInputChannels"])
            last_error: Exception | None = None
            for fmt_const, fmt_name in ((pyaudio.paInt16, "int16"), (pyaudio.paFloat32, "float32")):
                def callback(in_data, frame_count, time_info, status, _f=fmt_name):
                    on_pcm(in_data, rate, channels, _f)
                    return (None, pyaudio.paContinue)

                try:
                    stream = pa.open(
                        format=fmt_const,
                        channels=channels,
                        rate=rate,
                        frames_per_buffer=max(rate // 100, 64),  # ~10 ms
                        input=True,
                        input_device_index=info["index"],
                        stream_callback=callback,
                    )
                    return _WasapiStream(pa, stream, rate, channels, fmt_name)
                except Exception as e:  # try the next sample format
                    last_error = e
            raise RuntimeError(str(last_error))
        except Exception:
            pa.terminate()
            raise


def run_probe_cli() -> int:
    """Entry point for `--probe`: print one JSON line and exit."""
    try:
        res = probe_in_process()
        print(json.dumps({
            "devices": [d.__dict__ for d in res.devices],
            "default_name": res.default_name,
        }))
        return 0
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 1
