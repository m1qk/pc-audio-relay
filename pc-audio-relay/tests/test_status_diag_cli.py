import json
import subprocess
import sys
from types import SimpleNamespace

from pc_audio_relay import diagnostics as d
from pc_audio_relay.__main__ import apply_overrides, build_parser
from pc_audio_relay.config import Settings
from pc_audio_relay.netinfo import LanAddress
from pc_audio_relay.qr import ascii_qr, qr_matrix
from pc_audio_relay.status import compute_status


# ------------------------------- status ------------------------------------ #
def snap(cstate="running", clients=None, offers=0, rejected=0, device="Speakers", detail=""):
    return {"capture": {"state": cstate, "detail": detail, "device": device}, "clients": clients or [],
            "offers": offers, "rejected": rejected}


def test_status_stopped():
    assert compute_status(False, {}).level == "idle"


def test_status_waiting_then_hint_when_no_phone_ever_arrives():
    assert compute_status(True, snap(), 5).text == "Ready"
    s = compute_status(True, snap(), 60)
    assert s.text == "Waiting for your iPhone" and "same Wi-Fi" in s.detail


def test_status_connected_and_failed():
    ok = compute_status(True, snap(clients=[{"remote": "10.0.0.5", "connectionState": "connected"}], offers=1))
    assert ok.level == "ok" and "10.0.0.5" in ok.text and "Speakers" in ok.detail
    bad = compute_status(True, snap(clients=[{"remote": "10.0.0.5", "connectionState": "failed", "reason": "firewall"}], offers=1))
    assert bad.level == "error" and bad.detail == "firewall"
    con = compute_status(True, snap(clients=[{"remote": "10.0.0.5", "connectionState": "connecting"}], offers=1))
    assert con.level == "connecting"


def test_status_capture_problems_win_over_clients():
    assert compute_status(True, snap("error", detail="boom")).level == "error"
    w = compute_status(True, snap("waiting", detail="device not found: DAC"))
    assert w.level == "warn" and "DAC" in w.detail


def test_status_wrong_key():
    assert compute_status(True, snap(rejected=2)).text == "Pairing key rejected"


def test_status_underruns_warning():
    s = compute_status(True, snap(clients=[{"remote": "x", "connectionState": "connected", "underruns": 50}], offers=1))
    assert "breaking up" in s.detail


# ------------------------------ diagnostics -------------------------------- #
def cp(out="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr="")


ADDR = LanAddress("192.168.1.20", "Wi-Fi", False, True)


def test_public_network_profile_is_flagged_with_fix():
    run = lambda cmd: cp(json.dumps({"InterfaceAlias": "Wi-Fi", "NetworkCategory": 0}))
    c = d.check_network_profile(ADDR, run, "win32")
    assert c.status == "warn" and "Private" in c.fix
    run = lambda cmd: cp(json.dumps([{"InterfaceAlias": "Ethernet", "NetworkCategory": 0},
                                     {"InterfaceAlias": "Wi-Fi", "NetworkCategory": 1}]))
    assert d.check_network_profile(ADDR, run, "win32").status == "ok"


def test_network_profile_skipped_off_windows_and_tolerates_garbage():
    assert d.check_network_profile(ADDR, platform="linux") is None
    assert d.check_network_profile(ADDR, lambda c: cp("not json"), "win32").status == "info"


def test_firewall_rule_present_or_missing():
    assert d.check_firewall(lambda c: cp(rc=0), "win32").status == "ok"
    missing = d.check_firewall(lambda c: cp("No rules match", rc=1), "win32")
    assert missing.status == "warn" and "--fix-firewall" in missing.fix
    assert d.check_firewall(platform="linux") is None


def test_firewall_commands_cover_private_and_domain_for_each_program():
    cmds = d.firewall_commands()
    assert cmds[0][:5] == ["netsh", "advfirewall", "firewall", "delete", "rule"]
    adds = cmds[1:]
    assert adds and all("action=allow" in c and "dir=in" in c and "profile=private,domain" in c for c in adds)
    assert any(f"program={sys.executable}" in c for c in adds)


def test_address_checks():
    assert d.check_address(None).status == "fail"
    assert d.check_address(LanAddress("172.30.0.1", "vEthernet (WSL)", True)).status == "warn"
    assert d.check_address(LanAddress("8.8.4.4", "Wi-Fi", False)).status == "warn"
    assert d.check_address(ADDR).status == "ok"


def test_port_in_use_is_reported():
    import socket
    s = socket.socket(); s.bind(("0.0.0.0", 0)); s.listen(1)
    port = s.getsockname()[1]
    try:
        c = d.check_port(port, server_running=False)
        assert c.status == "fail" and "Another program" in c.fix
    finally:
        s.close()
    assert d.check_port(port, server_running=False).status == "ok"


# ---------------------------------- qr -------------------------------------- #
def test_qr_matrix_is_square_with_finder_patterns():
    m = qr_matrix("http://192.168.1.20:8765/?k=abc123")
    n = len(m)
    assert all(len(r) == n for r in m) and n >= 21
    assert all(m[0][:7]) and all(m[0][-7:]) and all(m[6][:7])  # top finder pattern edges


def test_ascii_qr_dimensions():
    art = ascii_qr("hello", quiet=2).split("\n")
    n = len(qr_matrix("hello"))
    assert len(art) == n + 4 and all(len(r) == 2 * (n + 4) for r in art)


# ---------------------------------- cli -------------------------------------- #
def test_cli_overrides_are_clamped():
    args = build_parser().parse_args(["--port", "80", "--buffer", "5000", "--bitrate", "1", "--no-key", "--device", "DAC"])
    s = apply_overrides(Settings(key="k"), args)
    assert (s.port, s.capture_buffer_ms, s.bitrate_kbps, s.require_key, s.device) == (1024, 300, 32, False, "DAC")


def test_settings_roundtrip_and_corrupt_file(tmp_path):
    p = tmp_path / "settings.json"
    s = Settings(port=9000, device="DAC", capture_buffer_ms=80).clamp()
    s.save(p)
    s2 = Settings.load(p)
    assert (s2.port, s2.device, s2.capture_buffer_ms, s2.key) == (9000, "DAC", 80, s.key)
    p.write_text("{ nope")
    assert Settings.load(p).port == 8765  # falls back to defaults, key regenerated
    p.write_text(json.dumps({"port": 9100, "bogus": 1}))
    assert Settings.load(p).port == 9100


def test_probe_subcommand_emits_json_and_never_crashes():
    out = subprocess.run([sys.executable, "-m", "pc_audio_relay", "--probe"], capture_output=True, text=True, timeout=30)
    last = out.stdout.strip().splitlines()[-1]
    data = json.loads(last)
    assert "error" in data or "devices" in data  # on Linux: a clean error; on Windows: devices


def test_console_tone_mode_prints_qr_and_url_then_exits_on_sigint():
    import os, signal, time
    p = subprocess.Popen([sys.executable, "-m", "pc_audio_relay", "--tone", "--port", "18765", "--no-key"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(3)
        os.kill(p.pid, signal.SIGINT)
        out, _ = p.communicate(timeout=15)
    finally:
        if p.poll() is None:
            p.kill()
    assert "http://" in out and ":18765/" in out and "█" in out
    assert "Stopping" in out and p.returncode == 0
