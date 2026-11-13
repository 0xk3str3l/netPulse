"""Startup capability detection: root, raw sockets, and iptables.

This module only detects and logs. The daemon uses the result to skip
attempting a subsystem outright rather than letting it fail: ARP discovery
and iptables accounting are the two things in Netpulse that need root (or
the equivalent capabilities), and neither being available should ever take
the whole daemon down -- presence checks (system `ping`) and interface-level
bandwidth (psutil) don't need root and keep running regardless.
"""

import logging
import os
import shutil
import socket
from dataclasses import dataclass

log = logging.getLogger("netpulse")


def has_root() -> bool:
    """Root on POSIX; elevation (Administrator) on Windows."""
    if os.name == "nt":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _npcap_available() -> bool:
    """True when Npcap's wpcap.dll is installed (needed for L2 on Windows).

    Checked on the filesystem rather than by importing scapy: importing scapy
    without Npcap prints a libpcap warning, and a capability probe should be
    silent.
    """
    import ctypes.util

    try:
        if ctypes.util.find_library("wpcap"):
            return True
    except Exception:
        pass
    windir = os.environ.get("SystemRoot", r"C:\Windows")
    candidates = (
        os.path.join(windir, "System32", "Npcap", "wpcap.dll"),
        os.path.join(windir, "System32", "wpcap.dll"),
    )
    return any(os.path.exists(path) for path in candidates)


def has_raw_socket() -> bool:
    """Best-effort check for L2 raw socket capability.

    Linux needs root or CAP_NET_RAW for AF_PACKET. Windows needs Npcap (scapy
    cannot send L2 frames without it) *and* elevation. When this is False the
    discoverer still works unprivileged via the OS ARP table.
    """
    af_packet = getattr(socket, "AF_PACKET", None)
    if af_packet is None:
        if os.name == "nt":
            return _npcap_available() and has_root()
        return has_root()
    try:
        raw = socket.socket(af_packet, socket.SOCK_RAW, socket.htons(0x0003))
        raw.close()
        return True
    except OSError:
        return False


def has_iptables() -> bool:
    return shutil.which("iptables") is not None


@dataclass
class Capabilities:
    root: bool
    raw_socket: bool
    iptables_binary: bool


def check_capabilities(use_iptables: bool) -> Capabilities:
    """Detect available privileges/tools and log what will be degraded.

    Never raises -- a missing capability is reported once here and the
    affected subsystem is disabled by the caller, not discovered later as
    a crash or a wall of per-cycle warnings.
    """
    caps = Capabilities(
        root=has_root(),
        raw_socket=has_raw_socket(),
        iptables_binary=has_iptables(),
    )

    if not caps.root:
        log.warning(
            "Not running as root/Administrator: L2 ARP sweeps and iptables "
            "accounting are unavailable; discovery falls back to the OS ARP "
            "table and interface bandwidth still works."
        )

    if not caps.raw_socket:
        log.warning(
            "No L2 raw-socket capability: falling back to the OS ARP table "
            "(zero-packet reads) plus rate-limited ICMP sweeps for discovery. "
            "Presence checks and interface-level bandwidth still run."
        )

    if use_iptables and not caps.iptables_binary:
        log.warning(
            "iptables not found on PATH: per-device bandwidth accounting "
            "disabled, falling back to interface-level bandwidth only."
        )

    return caps
