"""Command line entry point.

    python -m pc_audio_relay                 open the window (default)
    python -m pc_audio_relay --console       run without a window, print the QR code
    python -m pc_audio_relay --list-devices  show playback devices
    python -m pc_audio_relay --diagnose      check network + firewall
    python -m pc_audio_relay --fix-firewall  add Windows Firewall rules (asks for admin)
    python -m pc_audio_relay --tone          stream a test tone instead of PC audio
"""
from __future__ import annotations

import sys

# `--probe` is spawned many times by the capture manager, so handle it before
# importing anything heavy.
if "--probe" in sys.argv[1:]:
    from .wasapi import run_probe_cli
    raise SystemExit(run_probe_cli())

import argparse
import time

from .config import APP_NAME, VERSION, Settings


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pc_audio_relay", description=f"{APP_NAME} {VERSION}")
    p.add_argument("--console", action="store_true", help="run without a window")
    p.add_argument("--tone", nargs="?", const="tone", choices=["tone", "click", "noise"],
                   help="stream a test signal instead of capturing PC audio (tone=440/880 Hz stereo, "
                        "click=1 click per second for latency tests)")
    p.add_argument("--list-devices", action="store_true", help="list playback devices and exit")
    p.add_argument("--diagnose", action="store_true", help="run network and firewall checks and exit")
    p.add_argument("--fix-firewall", action="store_true", help="add Windows Firewall allow rules (UAC prompt)")
    p.add_argument("--device", help="playback device to capture (default: follow the Windows default)")
    p.add_argument("--port", type=int, help="HTTP port (default 8765)")
    p.add_argument("--address", help="PC IPv4 address to put in the QR code")
    p.add_argument("--buffer", type=int, metavar="MS", help="sender buffer in ms (10-300, default 40)")
    p.add_argument("--bitrate", type=int, metavar="KBPS", help="Opus bitrate (32-320, default 128)")
    p.add_argument("--no-key", action="store_true", help="do not require the pairing key in the URL")
    p.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    return p


def apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    if args.device is not None:
        settings.device = args.device
    if args.port is not None:
        settings.port = args.port
    if args.address is not None:
        settings.address = args.address
    if args.buffer is not None:
        settings.capture_buffer_ms = args.buffer
    if args.bitrate is not None:
        settings.bitrate_kbps = args.bitrate
    if args.no_key:
        settings.require_key = False
    return settings.clamp()


def cmd_list_devices() -> int:
    from .wasapi import WasapiBackend
    try:
        res = WasapiBackend().probe()
    except Exception as e:
        print(f"Could not list audio devices: {e}", file=sys.stderr)
        if sys.platform != "win32":
            print("(Device capture only works on Windows. Use --tone to test streaming elsewhere.)", file=sys.stderr)
        return 1
    for d in res.devices:
        mark = "  <- Windows default" if d.name == res.default_name else ""
        print(f"{d.name}  ({d.channels} ch, {d.rate} Hz){mark}")
    return 0


def cmd_diagnose(settings: Settings) -> int:
    from .diagnostics import run_diagnostics
    from .netinfo import pick_address
    checks = run_diagnostics(pick_address(settings.address), settings.port, server_running=False)
    for c in checks:
        print(c)
    return 1 if any(c.status == "fail" for c in checks) else 0


def run_console(settings: Settings, tone: str | None) -> int:
    from .app import RelayApp, StartError
    from .qr import ascii_qr
    from .status import compute_status

    app = RelayApp(settings, tone_mode=tone)
    try:
        app.start()
    except StartError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    t0 = time.time()
    url = app.url()
    print(f"\n{APP_NAME} {VERSION}")
    print(ascii_qr(url))
    print(f"\nOn your iPhone, scan the QR code with the Camera app, or open:\n\n    {url}\n")
    print(f"(Or by name: {app.alt_url()})")
    print("Press Ctrl+C to stop.\n")
    last = None
    try:
        while True:
            try:
                while True:
                    kind, msg = app.events.get_nowait()
                    if kind != "state":
                        print(f"[{time.strftime('%H:%M:%S')}] {kind}: {msg}")
            except Exception:
                pass
            st = compute_status(True, app.snapshot(), time.time() - t0)
            line = f"{st.text}. {st.detail}".strip()
            if line != last:
                print(f"[{time.strftime('%H:%M:%S')}] status: {line}")
                last = line
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        app.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = apply_overrides(Settings.load(), args)

    if args.list_devices:
        return cmd_list_devices()
    if args.diagnose:
        return cmd_diagnose(settings)
    if args.fix_firewall:
        from .diagnostics import fix_firewall_elevated
        ok, msg = fix_firewall_elevated()
        print(msg)
        return 0 if ok else 1
    if args.console or args.tone:
        return run_console(settings, args.tone)

    try:
        from .gui import run_gui
    except ImportError as e:  # tkinter missing
        print(f"The window needs tkinter ({e}). Falling back to console mode.\n", file=sys.stderr)
        return run_console(settings, None)
    return run_gui(settings, tone=args.tone)


if __name__ == "__main__":
    raise SystemExit(main())
