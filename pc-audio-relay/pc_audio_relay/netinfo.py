"""Finding the address the phone should connect to."""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Optional

import psutil

_VIRTUAL = re.compile(
    r"(vethernet|virtualbox|vmware|vmnet|hyper-v|wsl|docker|tailscale|zerotier|"
    r"tap-|tun|wireguard|openvpn|vpn|npcap|loopback|bluetooth|teredo|isatap|"
    r"pseudo|host-only|vbox)",
    re.I,
)


@dataclass
class LanAddress:
    ip: str
    interface: str
    virtual: bool
    is_default_route: bool = False

    @property
    def label(self) -> str:
        tags = []
        if self.is_default_route:
            tags.append("recommended")
        if self.virtual:
            tags.append("virtual/VPN")
        suffix = f"  ({', '.join(tags)})" if tags else ""
        return f"{self.ip}  [{self.interface}]{suffix}"


def default_route_ip() -> Optional[str]:
    """The local IP Windows would use to reach the internet/LAN. A UDP connect
    sends no packets; it only asks the OS to pick a route."""
    for probe in ("8.8.8.8", "192.168.0.1", "10.0.0.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 9))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("0."):
                return ip
        except OSError:
            continue
        finally:
            s.close()
    return None


def list_lan_addresses() -> list[LanAddress]:
    """Usable IPv4 addresses, best candidate first."""
    route_ip = default_route_ip()
    stats = psutil.net_if_stats()
    found: list[LanAddress] = []
    for ifname, addrs in psutil.net_if_addrs().items():
        st = stats.get(ifname)
        if st is not None and not st.isup:
            continue
        for a in addrs:
            if a.family != socket.AF_INET:
                continue
            try:
                ip = ipaddress.ip_address(a.address)
            except ValueError:
                continue
            if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
                continue
            found.append(LanAddress(
                ip=a.address,
                interface=ifname,
                virtual=bool(_VIRTUAL.search(ifname)),
                is_default_route=(a.address == route_ip),
            ))

    def rank(x: LanAddress):
        private = ipaddress.ip_address(x.ip).is_private
        return (not x.is_default_route, x.virtual, not private, x.ip)

    found.sort(key=rank)
    return found


def pick_address(preferred: Optional[str] = None) -> Optional[LanAddress]:
    addrs = list_lan_addresses()
    if preferred:
        for a in addrs:
            if a.ip == preferred:
                return a
    return addrs[0] if addrs else None


def build_url(ip: str, port: int, key: Optional[str]) -> str:
    base = f"http://{ip}:{port}/"
    return f"{base}?k={key}" if key else base


def hostname_url(port: int, key: Optional[str]) -> str:
    host = socket.gethostname()
    base = f"http://{host}.local:{port}/"
    return f"{base}?k={key}" if key else base
