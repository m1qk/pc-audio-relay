"""Network and Windows Firewall diagnostics, written to give an actionable fix.

Windows-only checks go through an injectable `run` function so they can be
tested on any OS with canned command output.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

from .config import FIREWALL_RULE_NAME
from .netinfo import LanAddress, list_lan_addresses

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | info
    message: str
    fix: str = ""

    def __str__(self) -> str:
        icon = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]", "info": "[INFO]"}[self.status]
        s = f"{icon} {self.name}: {self.message}"
        if self.fix:
            s += f"\n        Fix: {self.fix}"
        return s


def _default_run(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15, creationflags=flags)


def program_paths() -> list[str]:
    """Executables Windows Firewall might see as the listener. In a venv the
    launcher stub and the real interpreter differ, so cover both."""
    paths = []
    for p in (sys.executable, getattr(sys, "_base_executable", None)):
        if p and os.path.exists(p) and p not in paths:
            paths.append(p)
        # pythonw.exe is used when started without a console
        if p:
            w = os.path.join(os.path.dirname(p), "pythonw.exe")
            if os.path.exists(w) and w not in paths:
                paths.append(w)
    return paths


# --------------------------------------------------------------------------- #
def check_address(addr: Optional[LanAddress]) -> Check:
    if addr is None:
        return Check("Network", "fail", "No active network connection with an IPv4 address.",
                     "Connect the PC to the same Wi-Fi network as the iPhone (or plug in Ethernet on that router).")
    ip = ipaddress.ip_address(addr.ip)
    if addr.virtual:
        return Check("Network", "warn",
                     f"The address {addr.ip} belongs to '{addr.interface}', which looks like a VPN or virtual adapter.",
                     "Pick the Wi-Fi/Ethernet address in the address list, or disconnect the VPN while streaming.")
    if not ip.is_private:
        return Check("Network", "warn", f"{addr.ip} is not a private LAN address.",
                     "The phone and PC must be on the same local network.")
    return Check("Network", "ok", f"PC address {addr.ip} on '{addr.interface}'.")


def check_other_addresses(chosen: Optional[LanAddress]) -> Optional[Check]:
    others = [a for a in list_lan_addresses() if chosen is None or a.ip != chosen.ip]
    real = [a for a in others if not a.virtual]
    if real:
        return Check("Network", "info",
                     "This PC has more than one network connection: " + ", ".join(f"{a.ip} ({a.interface})" for a in real) +
                     ". The phone must be on the same network as the address in the QR code.")
    return None


def check_port(port: int, server_running: bool) -> Check:
    if server_running:
        return Check("Port", "ok", f"Listening on TCP {port}.")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return Check("Port", "ok", f"TCP {port} is free.")
    except OSError as e:
        return Check("Port", "fail", f"TCP {port} cannot be used: {e}",
                     "Another program is using it. Change the port in Settings or close that program.")
    finally:
        s.close()


def check_self_reachable(ip: str, port: int) -> Check:
    try:
        with urllib.request.urlopen(f"http://{ip}:{port}/api/info", timeout=3) as r:
            json.loads(r.read().decode())
        return Check("Web page", "ok", f"The receiver page answers on http://{ip}:{port}/.")
    except Exception as e:
        return Check("Web page", "fail", f"The PC could not open its own page at {ip}:{port} ({e}).",
                     "Start streaming first. If it is running, another program or security software may be interfering.")


def check_network_profile(addr: Optional[LanAddress], run: Runner = _default_run, platform: str = sys.platform) -> Optional[Check]:
    if platform != "win32" or addr is None:
        return None
    try:
        out = run(["powershell", "-NoProfile", "-Command",
                   "Get-NetConnectionProfile | Select-Object InterfaceAlias,NetworkCategory | ConvertTo-Json -Compress"])
        data = json.loads(out.stdout or "[]")
    except Exception:
        return Check("Network profile", "info", "Could not read the Windows network profile.")
    if isinstance(data, dict):
        data = [data]
    prof = next((d for d in data if d.get("InterfaceAlias") == addr.interface), None)
    if prof is None:
        return None
    cat = prof.get("NetworkCategory")
    # NetworkCategory may be serialised as a number (0 Public, 1 Private, 2 Domain) or a string
    name = {0: "Public", 1: "Private", 2: "DomainAuthenticated"}.get(cat, str(cat))
    if name == "Public":
        return Check("Network profile", "warn",
                     f"'{addr.interface}' is set to a Public network. Windows blocks incoming connections more strictly there.",
                     "Settings > Network & Internet > Wi-Fi > (your network) > Network profile type > Private.")
    return Check("Network profile", "ok", f"'{addr.interface}' is a {name} network.")


def firewall_rule_exists(run: Runner = _default_run) -> bool:
    try:
        out = run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={FIREWALL_RULE_NAME}"])
        return out.returncode == 0
    except Exception:
        return False


def check_firewall(run: Runner = _default_run, platform: str = sys.platform) -> Optional[Check]:
    if platform != "win32":
        return None
    if firewall_rule_exists(run):
        return Check("Firewall", "ok", f"A Windows Firewall rule named '{FIREWALL_RULE_NAME}' exists.")
    return Check("Firewall", "warn",
                 "No firewall rule for this app yet. If Windows showed a firewall prompt and you chose Cancel or "
                 "only allowed Public networks, the phone will reach the page but audio will not connect.",
                 "Press 'Fix firewall' (needs administrator approval), or run: python -m pc_audio_relay --fix-firewall")


def firewall_commands() -> list[list[str]]:
    cmds = [["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE_NAME}"]]
    for path in program_paths():
        cmds.append(["netsh", "advfirewall", "firewall", "add", "rule", f"name={FIREWALL_RULE_NAME}",
                     "dir=in", "action=allow", f"program={path}", "enable=yes",
                     "profile=private,domain", "protocol=any"])
    return cmds


def fix_firewall_elevated() -> tuple[bool, str]:
    """Ask Windows for administrator rights (UAC prompt) and add the allow rules."""
    if sys.platform != "win32":
        return False, "Firewall rules are only needed on Windows."
    import ctypes

    parts = []
    for c in firewall_commands():
        parts.append(" ".join(f'"{x}"' if " " in x else x for x in c))
    # one elevated cmd.exe runs every rule; '&' keeps going even if the delete finds nothing
    params = '/c ' + " & ".join(parts)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", params, None, 0)
    if rc <= 32:
        return False, "Administrator approval was cancelled or failed."
    return True, "Requested. After approving the Windows prompt, press Diagnose to confirm."


def run_diagnostics(addr: Optional[LanAddress], port: int, server_running: bool,
                    run: Runner = _default_run, platform: str = sys.platform) -> list[Check]:
    checks = [check_address(addr)]
    other = check_other_addresses(addr)
    if other:
        checks.append(other)
    checks.append(check_port(port, server_running))
    if server_running and addr:
        checks.append(check_self_reachable(addr.ip, port))
    for c in (check_network_profile(addr, run, platform), check_firewall(run, platform)):
        if c:
            checks.append(c)
    checks.append(Check("Wi-Fi", "info",
                        "If the page opens on the iPhone but audio never connects, check that the router has "
                        "'AP/client isolation' turned off. Guest networks and some campus or hotel Wi-Fi block "
                        "device-to-device traffic."))
    return checks
