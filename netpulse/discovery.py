"""Network utilities and low-footprint device discovery.

Discovery has two backends:

* L2 ARP broadcast sweep via scapy, when a raw socket / Npcap is available.
* The operating system's ARP/neighbour table (``arp -a`` on Windows,
  ``ip neigh`` on Linux), which costs **zero packets** to read.

The OS-table backend is what makes netpulse usable unprivileged and stealthy:
on a warm network it sees every recently-active host without emitting a
single frame. When the cache is cold (or every ``active_sweep_interval``),
``auto`` mode falls back to a rate-limited ICMP sweep to refill it.
"""

import logging
import os
import re
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from ipaddress import IPv4Network, ip_address
from typing import Dict, List, Optional, Tuple

try:  # optional: only needed for interface/CIDR auto-detection
    import netifaces
except ImportError:  # pragma: no cover - exercised by CI without runtime deps
    netifaces = None

from .oui import lookup_vendor

log = logging.getLogger("netpulse")

IS_WINDOWS = os.name == "nt"

_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
_MAC_RE = re.compile(r"\b([0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5})\b")


def _norm_mac(mac: str) -> str:
    return (mac or "").strip().lower().replace("-", ":")


def _is_multicast_mac(mac: str) -> bool:
    try:
        return bool(int(mac.split(":", 1)[0], 16) & 1)
    except (ValueError, IndexError):
        return True


def ping_command(ip: str, count: int = 1, timeout: float = 1.0) -> List[str]:
    """Build a platform-correct, single-shot ICMP command.

    Windows ``ping`` uses ``-n`` for count and ``-w`` in milliseconds;
    Linux/BSD use ``-c`` and ``-W`` in seconds.
    """
    count = max(1, int(count))
    if IS_WINDOWS:
        return ["ping", "-n", str(count), "-w", str(max(1, int(timeout * 1000))), ip]
    return ["ping", "-c", str(count), "-W", str(max(1, int(round(timeout)))), "-q", "-n", ip]


def _friendly_name(iface: str) -> str:
    """Map a netifaces GUID to the name psutil/scapy use on Windows."""
    if not IS_WINDOWS or netifaces is None:
        return iface
    try:
        addrs = netifaces.ifaddresses(iface)
    except (ValueError, OSError):
        return iface
    macs = {
        _norm_mac(a.get("addr", ""))
        for a in addrs.get(netifaces.AF_LINK, [])
        if a.get("addr")
    }
    if not macs:
        return iface
    try:
        import psutil
    except ImportError:  # pragma: no cover
        return iface
    for name, ps_addrs in psutil.net_if_addrs().items():
        if any(_norm_mac(a.address) in macs for a in ps_addrs if a.address):
            return name
    return iface


def _netifaces_name(iface: str) -> str:
    """Inverse of _friendly_name: accept either a Windows name or GUID."""
    if not IS_WINDOWS or netifaces is None:
        return iface
    try:
        netifaces.ifaddresses(iface)
        return iface
    except (ValueError, OSError):
        pass
    macs = set()
    try:
        import psutil
        macs = {_norm_mac(a.address) for a in psutil.net_if_addrs().get(iface, []) if a.address}
    except ImportError:  # pragma: no cover
        return iface
    for name in netifaces.interfaces():
        try:
            addrs = netifaces.ifaddresses(name).get(netifaces.AF_LINK, [])
        except (ValueError, OSError):
            continue
        if any(_norm_mac(a.get("addr", "")) in macs for a in addrs if a.get("addr")):
            return name
    return iface


def _psutil_default_interface() -> str:
    try:
        import psutil
        for name, addrs in psutil.net_if_addrs().items():
            if name.lower().startswith("lo"):
                continue
            if any(a.family == socket.AF_INET for a in addrs):
                return name
    except Exception:
        pass
    return "eth0" if not IS_WINDOWS else "Ethernet"


def get_default_interface() -> str:
    """Get the first non-loopback interface with a default route."""
    if netifaces is None:
        return _psutil_default_interface()
    gateways = netifaces.gateways()
    if "default" in gateways and netifaces.AF_INET in gateways["default"]:
        return _friendly_name(gateways["default"][netifaces.AF_INET][1])
    for iface in netifaces.interfaces():
        if iface == "lo":
            continue
        try:
            if netifaces.AF_INET in netifaces.ifaddresses(iface):
                return _friendly_name(iface)
        except (ValueError, OSError):
            continue
    return _psutil_default_interface()


def get_interface_ip_and_netmask(iface: str) -> Tuple[Optional[str], Optional[str]]:
    if netifaces is None:
        return None, None
    try:
        addrs = netifaces.ifaddresses(_netifaces_name(iface))
        if netifaces.AF_INET in addrs:
            info = addrs[netifaces.AF_INET][0]
            return info.get("addr"), info.get("netmask")
    except Exception:
        pass
    return None, None


def get_interface_cidr(iface: str) -> Optional[str]:
    ip, mask = get_interface_ip_and_netmask(iface)
    if ip and mask:
        try:
            # Convert dotted-decimal netmask to prefix length
            mask_bits = sum(bin(int(x)).count("1") for x in mask.split("."))
            network = ip_address(ip).network_address
            return str(IPv4Network(f"{network}/{mask_bits}", strict=False))
        except Exception:
            return None
    return None


def get_own_mac(iface: str) -> str:
    if netifaces is None:
        return ""
    try:
        addrs = netifaces.ifaddresses(_netifaces_name(iface))
        if netifaces.AF_LINK in addrs:
            return _norm_mac(addrs[netifaces.AF_LINK][0].get("addr", ""))
    except Exception:
        pass
    return ""


def resolve_hostname(ip: str) -> str:
    try:
        host, _, _ = socket.gethostbyaddr(ip)
        return host
    except (socket.herror, socket.gaierror, OSError):
        return ""


def parse_arp_table(text: str, subnet: str) -> List[Dict[str, str]]:
    """Parse Windows ``arp -a`` / Linux ``ip neigh`` / ``arp -an`` output.

    Only unicast IPv4 entries inside ``subnet`` are returned, de-duplicated by
    MAC. This is pure parsing: it emits no traffic and is trivially testable.
    """
    network = IPv4Network(subnet, strict=False)
    devices: Dict[str, Dict[str, str]] = {}
    for line in text.splitlines():
        ip_match = _IPV4_RE.search(line)
        mac_match = _MAC_RE.search(line)
        if not ip_match or not mac_match:
            continue
        ip = ip_match.group(1)
        mac = _norm_mac(mac_match.group(1))
        try:
            addr = ip_address(ip)
        except ValueError:
            continue
        if addr not in network or addr == network.broadcast_address:
            continue
        if addr.is_multicast or _is_multicast_mac(mac):
            continue
        if mac in devices:
            continue
        devices[mac] = {"ip": ip, "mac": mac}
    return list(devices.values())


class ARPDiscoverer:
    """Low-footprint discovery: L2 ARP when possible, OS ARP cache otherwise."""

    def __init__(self, interface: str, timeout: float = 3.0, enabled: bool = True,
                 mode: str = "auto", sweep_rate: int = 32,
                 active_sweep_interval: int = 900,
                 exclude_macs: Optional[List[str]] = None,
                 exclude_ips: Optional[List[str]] = None):
        self.interface = interface
        self.timeout = timeout
        self.enabled = enabled          # raw L2 capability, from capabilities.py
        self.mode = mode
        self.sweep_rate = max(1, int(sweep_rate))
        self.active_sweep_interval = max(0, int(active_sweep_interval))
        self.exclude_macs = {_norm_mac(m) for m in (exclude_macs or [])}
        self.exclude_ips = {str(ip_address(i)) for i in (exclude_ips or []) if "/" not in i}
        self.own_mac = get_own_mac(interface)
        self.own_ip = get_interface_ip_and_netmask(interface)[0]
        self._raw_ok = bool(enabled)
        # Start "fresh": a warm OS cache should not trigger an immediate sweep
        # just because the epoch is older than active_sweep_interval.
        self._last_active_sweep = time.time()
        self._last_table_warning = 0.0

    # ── public ─────────────────────────────────────────────────────────────

    def sweep(self, subnet: str) -> List[Dict]:
        """Discover devices on ``subnet`` according to the configured mode."""
        try:
            network = IPv4Network(subnet, strict=False)
        except ValueError:
            log.warning(f"Invalid subnet {subnet!r}; skipping discovery")
            return []

        if self.mode != "passive" and self._raw_ok:
            devices = self._l2_sweep(network)
            if devices is not None:
                return self._filter(devices)

        # Zero-packet path: the OS already knows who has been talking.
        devices = self._arp_table(network)

        if self.mode != "passive" and self._active_due(devices):
            self._icmp_sweep(network)
            devices = self._arp_table(network)

        filtered = self._filter(devices)
        log.debug(
            f"Discovery ({self.mode}) found {len(filtered)} device(s) in {subnet}"
        )
        return filtered

    # ── backends ───────────────────────────────────────────────────────────

    def _l2_sweep(self, network: IPv4Network) -> Optional[List[Dict]]:
        """Broadcast ARP sweep. Returns None when the raw backend is unusable."""
        try:
            from scapy.all import ARP, Ether, conf, srp
        except ImportError:  # pragma: no cover
            self._raw_ok = False
            return None
        try:
            conf.iface = self.interface
            conf.verb = 0  # completely silent
            ans, _ = srp(
                Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(pdst=str(network)),
                timeout=self.timeout,
                iface=self.interface,
                inter=0.01,       # 10ms between packets
                verbose=False,
                retry=1,
            )
            devices = []
            seen = set()
            for _sent, recv in ans:
                mac = _norm_mac(recv[Ether].src)
                if mac in seen or mac == self.own_mac:
                    continue
                seen.add(mac)
                devices.append({"ip": recv[ARP].psrc, "mac": mac})
            return devices
        except Exception as e:
            # Log once, then quietly use the OS cache for the rest of the run
            # instead of warning every discovery cycle.
            self._raw_ok = False
            log.warning(
                f"L2 ARP sweep unavailable ({e}); switching to the OS ARP table "
                "and rate-limited ICMP sweeps"
            )
            return None

    def _arp_table(self, network: IPv4Network) -> List[Dict]:
        cmd = ["arp", "-a"] if IS_WINDOWS else ["ip", "neigh", "show"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0 and not IS_WINDOWS:
                result = subprocess.run(
                    ["arp", "-an"], capture_output=True, text=True, timeout=10
                )
            if result.returncode != 0:
                self._warn_table(result.stderr)
                return []
            return parse_arp_table(result.stdout, str(network))
        except (OSError, subprocess.SubprocessError) as e:
            self._warn_table(str(e))
            return []

    def _warn_table(self, detail: str) -> None:
        now = time.time()
        if now - self._last_table_warning > 300:
            log.warning(f"Could not read the OS ARP table: {detail.strip() or detail}")
            self._last_table_warning = now

    def _active_due(self, devices: List[Dict]) -> bool:
        if self.mode == "active":
            return True
        if not devices:
            return True
        return (time.time() - self._last_active_sweep) >= self.active_sweep_interval

    def _icmp_sweep(self, network: IPv4Network) -> None:
        """Rate-limited ICMP probe of the subnet to repopulate the ARP cache.

        Deliberately boring traffic: at the default 32 probes/sec a /24 takes
        ~8s and is indistinguishable from a normal admin ping sweep. Skipped
        entirely in passive mode.
        """
        hosts = [str(h) for h in network.hosts() if str(h) != self.own_ip]
        if not hosts:
            return
        rate = min(self.sweep_rate, 128)
        workers = min(rate, len(hosts))
        log.debug(f"Active ICMP sweep: {len(hosts)} host(s) at <= {rate}/s")
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for start in range(0, len(hosts), rate):
                    batch = hosts[start:start + rate]
                    list(pool.map(self._probe, batch))
                    if start + rate < len(hosts):
                        time.sleep(1.0)
        finally:
            self._last_active_sweep = time.time()

    @staticmethod
    def _probe(ip: str) -> None:
        try:
            subprocess.run(
                ping_command(ip, count=1, timeout=0.3),
                capture_output=True, text=True, timeout=1.5,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def _filter(self, devices: List[Dict]) -> List[Dict]:
        out = []
        seen = set()
        for device in devices:
            mac = _norm_mac(device.get("mac", ""))
            ip = device.get("ip", "")
            if not mac or not ip or mac in seen:
                continue
            if mac == self.own_mac or ip == self.own_ip:
                continue
            if mac in self.exclude_macs or ip in self.exclude_ips:
                continue
            seen.add(mac)
            out.append({"ip": ip, "mac": mac, "vendor": lookup_vendor(mac)})
        return out
