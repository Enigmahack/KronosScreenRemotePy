"""Which instrument a connection is talking to, as a file-name-safe key.

The daemon reports nothing per-unit (api.md: MODEL / SYSINFO / discovery are the same on every unit of a model, and the
default logins are shared), so the unit is identified by the MAC address the OS has in its ARP cache for the host: the
connection itself has just filled it. Same-subnet only; when no usable MAC is found (other subnet, lookup failed) the key
falls back to `host_<address>`, which can never be mistaken for a MAC key.

The OS-specific part is `parse_arp` (pure: feed it any platform's text) and `_arp_text` (one branch per OS)."""
from __future__ import annotations

import re
import socket
import subprocess
import sys
import threading
from typing import Dict, Optional

_MAC = re.compile(r"(?<![0-9A-Fa-f])((?:[0-9A-Fa-f]{1,2}[:-]){5}[0-9A-Fa-f]{1,2})(?![0-9A-Fa-f])")
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _norm_mac(raw: str) -> str:
    """aa-bb-cc-dd-ee-ff / aa:b:cc:... (macOS drops leading zeros) -> AABBCCDDEEFF, '' if not a unicast MAC."""
    parts = re.split(r"[:-]", raw)
    if len(parts) != 6:
        return ""
    mac = "".join(p.zfill(2) for p in parts).upper()
    first = int(mac[:2], 16)
    if mac == "0" * 12 or first & 1:          # incomplete entry, or broadcast / multicast
        return ""
    return mac


def parse_arp(text: str, ip: str) -> str:
    """MAC for `ip` from Linux /proc/net/arp, Windows `arp -a` or macOS/BSD `arp -an` text. '' if absent or ambiguous:
    a MAC listed for more than one IP is a router answering for other subnets, not this unit."""
    macs: Dict[str, set] = {}
    for line in (text or "").splitlines():
        ips, m = _IPV4.findall(line), _MAC.search(line)
        if not ips or not m:
            continue
        mac = _norm_mac(m.group(1))
        if mac:
            macs.setdefault(mac, set()).add(ips[0])
    mine = [mac for mac, addrs in macs.items() if ip in addrs]
    return mine[0] if len(mine) == 1 and macs[mine[0]] == {ip} else ""


def _arp_text(ip: str) -> str:
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/net/arp", encoding="ascii", errors="replace") as f:
                return f.read()
        except OSError:
            pass
    cmd = ["arp", "-a"] if sys.platform == "win32" else ["arp", "-an"]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3, creationflags=flags).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


_cache: Dict[str, str] = {}
_lock = threading.Lock()


def mac_for_host(host: str) -> str:
    """Cached ARP lookup (blocks up to a few seconds the first time; `warm` does it off-thread)."""
    with _lock:
        if host in _cache:
            return _cache[host]
    try:
        ip = socket.gethostbyname(host)
    except OSError:
        return ""
    mac = parse_arp(_arp_text(ip), ip)
    if mac:                                    # never cache a miss: the entry may simply not exist yet
        with _lock:
            _cache[host] = mac
    return mac


def warm(host: str) -> None:
    """Fill the cache on a worker thread (call right after connecting, when the ARP entry is fresh)."""
    if host:
        threading.Thread(target=mac_for_host, args=(host,), daemon=True, name="ArpLookup").start()


def device_key(host: str, mac: Optional[str] = None) -> str:
    """Name for this instrument's cache: `MAC_<12 hex>`, else `host_<host>`. Always safe as a file name."""
    mac = mac if mac is not None else mac_for_host(host)
    if mac:
        return "MAC_" + mac
    return "host_" + (_UNSAFE.sub("_", (host or "").strip()) or "unknown")


def _selftest() -> None:
    ip = "192.168.100.15"
    win = f"Interface: 192.168.100.9 --- 0x5\n  Internet Address      Physical Address      Type\n  {ip}   aa-bb-cc-dd-ee-0f     dynamic\n  192.168.100.1   00-11-22-33-44-55   dynamic\n  224.0.0.22   01-00-5e-00-00-16   static\n"
    lin = f"IP address       HW type     Flags       HW address            Mask     Device\n{ip}    0x1         0x2         aa:bb:cc:dd:ee:0f     *        eth0\n"
    mac = f"? ({ip}) at aa:bb:cc:dd:ee:f on en0 ifscope [ethernet]\n"
    assert parse_arp(win, ip) == parse_arp(lin, ip) == parse_arp(mac, ip) == "AABBCCDDEE0F"
    assert parse_arp(win, "192.168.100.77") == "" and parse_arp("", ip) == ""
    assert parse_arp(f"{ip} 0x1 0x0 00:00:00:00:00:00 * eth0", ip) == ""                      # incomplete entry
    router = f"{ip} 0x1 0x2 00:11:22:33:44:55 * e\n10.0.0.2 0x1 0x2 00:11:22:33:44:55 * e\n"   # one MAC, two IPs
    assert parse_arp(router, ip) == ""
    assert device_key("h", "AABBCCDDEE0F") == "MAC_AABBCCDDEE0F" and device_key("h", "") == "host_h"
    assert "/" not in device_key("..\\x/y", "") and device_key("", "") == "host_unknown"


if __name__ == "__main__":
    _selftest()
    print("device_identity self-test passed")
