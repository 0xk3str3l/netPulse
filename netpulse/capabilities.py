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
    return hasattr(os, "geteuid") and os.geteuid() == 0


def has_raw_socket() -> bool:
    """Best-effort check for L2 raw socket capability (root or CAP_NET_RAW).

    This is what the ARP sweep needs on Linux. Non-Linux platforms have no
    AF_PACKET, so fall back to a plain root check there.
    """
    af_packet = getattr(socket, "AF_PACKET", None)
    if af_packet is None:
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
            "Not running as root: ARP discovery and iptables accounting "
            "typically need root (or CAP_NET_RAW/CAP_NET_ADMIN)."
        )

    if not caps.raw_socket:
        log.warning(
            "No raw-socket capability: ARP-based device discovery is disabled "
            "for this run. Presence checks and interface-level bandwidth for "
            "already-known devices still run."
        )

    if use_iptables and not caps.iptables_binary:
        log.warning(
            "iptables not found on PATH: per-device bandwidth accounting "
            "disabled, falling back to interface-level bandwidth only."
        )

    return caps
