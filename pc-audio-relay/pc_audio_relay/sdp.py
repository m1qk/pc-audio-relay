"""Small SDP text helpers.

Why these exist:
* Browsers hide their LAN address behind a random "xxxx.local" mDNS name. The
  server can't always resolve those, so we substitute the IP the HTTP request
  actually came from (same device, same Wi-Fi).
* aiortc advertises every interface (VPNs, WSL, Hyper-V...). We only advertise
  the LAN address that is shown in the QR code.
* Opus is always negotiated as "opus/48000/2", but browsers decode it as MONO
  unless `stereo=1` appears in the SDP, so we add it.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Iterable, Optional

_CAND_RE = re.compile(r"^a=candidate:(\S+) (\d+) (\S+) (\d+) (\S+) (\d+) typ (\S+)(.*)$")


def _lines(sdp: str) -> list[str]:
    return sdp.replace("\r\n", "\n").split("\n")


def _join(lines: Iterable[str]) -> str:
    out = "\r\n".join(lines)
    return out if out.endswith("\r\n") else out + "\r\n"


def _is_ipv4(addr: Optional[str]) -> bool:
    try:
        return isinstance(ipaddress.ip_address(addr or ""), ipaddress.IPv4Address)
    except ValueError:
        return False


def rewrite_mdns_candidates(sdp: str, peer_ip: Optional[str]) -> str:
    """Replace `*.local` candidate hostnames with the peer's real IPv4 address.

    If the peer's address isn't IPv4 the mDNS candidates are dropped instead.
    Duplicates created by the substitution are removed.
    """
    out, seen = [], set()
    for line in _lines(sdp):
        m = _CAND_RE.match(line)
        if m and m.group(5).lower().endswith(".local"):
            if not _is_ipv4(peer_ip):
                continue
            foundation, comp, proto, prio, _addr, port, typ, rest = m.groups()
            key = (comp, proto.lower(), peer_ip, port)
            if key in seen:
                continue
            seen.add(key)
            line = f"a=candidate:{foundation} {comp} {proto} {prio} {peer_ip} {port} typ {typ}{rest}"
        out.append(line)
    return _join([l for l in out if l])


def filter_local_candidates(sdp: str, allowed_ips: set[str]) -> str:
    """Keep only candidates whose address is in allowed_ips.

    If that would remove *every* candidate the SDP is returned unchanged, so a
    wrong address setting degrades to "advertise everything" instead of "nothing".
    """
    lines = [l for l in _lines(sdp) if l]
    kept, removed_any, kept_any = [], False, False
    for line in lines:
        m = _CAND_RE.match(line)
        if m:
            if m.group(5) in allowed_ips:
                kept_any = True
                kept.append(line)
            else:
                removed_any = True
            continue
        kept.append(line)
    if removed_any and not kept_any:
        return _join(lines)
    return _join(kept)


def list_candidates(sdp: str) -> list[str]:
    """Human-readable 'ip:port/proto' for each candidate (for logs and diagnostics)."""
    res = []
    for line in _lines(sdp):
        m = _CAND_RE.match(line)
        if m:
            res.append(f"{m.group(5)}:{m.group(6)}/{m.group(3).lower()} ({m.group(7)})")
    return res


def add_opus_stereo(sdp: str) -> str:
    """Ensure every opus/48000/2 payload advertises stereo=1;sprop-stereo=1."""
    lines = [l for l in _lines(sdp) if l]
    pts = [m.group(1) for l in lines if (m := re.match(r"^a=rtpmap:(\d+) opus/48000/2", l, re.I))]
    for pt in pts:
        have_fmtp = False
        for i, line in enumerate(lines):
            if line.startswith(f"a=fmtp:{pt} "):
                have_fmtp = True
                params = line.split(" ", 1)[1]
                for p in ("stereo=1", "sprop-stereo=1"):
                    key = p.split("=")[0]
                    if not re.search(rf"(^|;)\s*{re.escape(key)}=", params):
                        params += ";" + p
                lines[i] = f"a=fmtp:{pt} {params}"
        if not have_fmtp:
            idx = next(i for i, l in enumerate(lines) if l.lower().startswith(f"a=rtpmap:{pt} opus"))
            lines.insert(idx + 1, f"a=fmtp:{pt} minptime=10;stereo=1;sprop-stereo=1")
    return _join(lines)


def keep_only_opus(sdp: str) -> str:
    """Drop every audio payload type except Opus from the m-line and attributes.
    Returns the SDP unchanged if no Opus payload is present."""
    lines = [l for l in _lines(sdp) if l]
    opus_pts = {m.group(1) for l in lines if (m := re.match(r"^a=rtpmap:(\d+) opus/", l, re.I))}
    if not opus_pts:
        return _join(lines)
    out = []
    for line in lines:
        if line.startswith("m=audio "):
            parts = line.split(" ")
            line = " ".join(parts[:3] + [p for p in parts[3:] if p in opus_pts])
        m = re.match(r"^a=(?:rtpmap|fmtp|rtcp-fb):(\d+)", line)
        if m and m.group(1) not in opus_pts:
            continue
        out.append(line)
    return _join(out)
