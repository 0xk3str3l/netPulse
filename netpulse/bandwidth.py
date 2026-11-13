"""Bandwidth tracking: interface-level via psutil, per-device via iptables.

Per-device accounting uses a dedicated chain (``NETPULSE_INPUT`` by default)
hooked into INPUT and FORWARD. Two counter-only rules are installed per known
device IP -- one matching as source (tx) and one as destination (rx) -- and
removed when the address disappears. The rules have no target, so they simply
count and fall through.
"""

import logging
import subprocess
import time
from ipaddress import ip_address
from typing import Dict, Optional, Set, Tuple

from .config import Config
from .db import Database

log = logging.getLogger("netpulse")


def _ip_token(value: str) -> Optional[str]:
    """Return the bare IP from an iptables source/destination token, or None."""
    token = value.split("/", 1)[0]
    try:
        addr = ip_address(token)
    except ValueError:
        return None
    return None if str(addr) == "0.0.0.0" else str(addr)


class BandwidthMonitor:
    """Tracks bandwidth at interface level (psutil) and per-device (iptables)."""

    def __init__(self, db: Database, config: Config, iface: str,
                 use_iptables: Optional[bool] = None):
        self.db = db
        self.config = config
        self.iface = iface
        self.prev_iface_counters: Dict[str, Tuple[int, int]] = {}
        self.prev_time = time.time()

        # iptables per-IP tracking. `use_iptables` lets the daemon pass a
        # capability-checked decision in; falls back to config.use_iptables
        # for direct/test construction.
        self.iptables_initialized = False
        self._tracked_ips: Set[str] = set()
        self._prev_device_counts: Dict[str, Tuple[int, int]] = {}
        self._prev_device_time: Optional[float] = None
        self._read_warned = False
        effective_use_iptables = config.use_iptables if use_iptables is None else use_iptables
        if effective_use_iptables:
            self._init_iptables()

    def _iptables(self, *args: str, check: bool = False):
        return subprocess.run(
            ["iptables", "-w", "2", *args],
            capture_output=True, text=True, check=check,
        )

    def _init_iptables(self):
        """Create iptables chain for per-IP accounting."""
        try:
            chain = self.config.iptables_chain
            # Create chain
            created = self._iptables("-N", chain)
            if created.returncode != 0 and "Chain already exists" not in created.stderr:
                log.warning(
                    f"iptables chain creation failed ({created.stderr.strip() or 'permission denied?'}); "
                    "per-device bandwidth accounting disabled (needs root)"
                )
                return

            # Add to INPUT and FORWARD if not already there
            for hook in ["INPUT", "FORWARD"]:
                existing = self._iptables("-C", hook, "-j", chain)
                if existing.returncode != 0:
                    inserted = self._iptables("-I", hook, "-j", chain)
                    if inserted.returncode != 0:
                        log.warning(
                            f"iptables rule insertion into {hook} failed "
                            f"({inserted.stderr.strip() or 'permission denied?'}); "
                            "per-device bandwidth accounting disabled"
                        )
                        return

            self.iptables_initialized = True
            log.info("iptables accounting chain initialized")
        except FileNotFoundError:
            log.warning("iptables not available; falling back to interface-level only")
        except Exception as e:
            log.warning(f"iptables init failed: {e}")

    def _ensure_device_rules(self, ip: str) -> None:
        """Install counter-only source/destination rules for one device IP."""
        if not self.iptables_initialized or ip in self._tracked_ips:
            return
        chain = self.config.iptables_chain
        for spec in (("-s", ip), ("-d", ip)):
            if self._iptables("-C", chain, *spec).returncode != 0:
                added = self._iptables("-A", chain, *spec)
                if added.returncode != 0:
                    if not self._read_warned:
                        log.warning(
                            f"iptables rule add failed ({added.stderr.strip() or 'permission denied?'}); "
                            "per-device bandwidth accounting unavailable"
                        )
                        self._read_warned = True
                    return
        self._tracked_ips.add(ip)

    def _remove_device_rules(self, ip: str) -> None:
        if not self.iptables_initialized:
            return
        chain = self.config.iptables_chain
        for spec in (("-s", ip), ("-d", ip)):
            self._iptables("-D", chain, *spec)
        self._tracked_ips.discard(ip)

    def _get_iptables_counts(self) -> Dict[str, Dict[str, int]]:
        """Read per-IP byte counts from the iptables accounting chain."""
        result: Dict[str, Dict[str, int]] = {}
        if not self.iptables_initialized:
            return result
        try:
            chain = self.config.iptables_chain
            output = self._iptables("-L", chain, "-n", "-v", "-x", check=True).stdout

            for line in output.splitlines():
                parts = line.split()
                if len(parts) < 7 or not parts[0].isdigit():
                    continue  # chain header / column header
                try:
                    bytes_val = int(parts[1])
                except ValueError:
                    continue
                src_ip = _ip_token(parts[-2])
                dst_ip = _ip_token(parts[-1])
                if src_ip:
                    result.setdefault(src_ip, {"rx": 0, "tx": 0})["tx"] += bytes_val
                if dst_ip:
                    result.setdefault(dst_ip, {"rx": 0, "tx": 0})["rx"] += bytes_val
            self._read_warned = False
        except Exception as e:
            if not self._read_warned:
                log.warning(f"iptables read failed: {e}")
                self._read_warned = True
        return result

    def sample_interface(self):
        """Sample interface-level bandwidth and store to DB."""
        import psutil  # lazy: keeps --export / unit tests dependency-free

        now = time.time()
        elapsed = now - self.prev_time
        if elapsed < 0.1:
            return

        try:
            counters = psutil.net_io_counters(pernic=True).get(self.iface)
            if not counters:
                return

            rx_bytes = counters.bytes_recv
            tx_bytes = counters.bytes_sent

            prev_rx, prev_tx = self.prev_iface_counters.get(self.iface, (rx_bytes, tx_bytes))
            rx_rate = max(0.0, (rx_bytes - prev_rx) / elapsed)
            tx_rate = max(0.0, (tx_bytes - prev_tx) / elapsed)

            self.db.record_interface_bw(self.iface, rx_bytes, tx_bytes, rx_rate, tx_rate)

            self.prev_iface_counters[self.iface] = (rx_bytes, tx_bytes)
            self.prev_time = now

        except Exception as e:
            log.warning(f"Interface bandwidth sample failed: {e}")

    def sample_per_device(self, devices: Dict[str, Dict]):
        """Map iptables byte counters to known devices and store deltas."""
        if not self.iptables_initialized:
            return

        now = time.time()
        ip_to_mac: Dict[str, str] = {}
        for mac, device in devices.items():
            ip = device.get("ip")
            if not ip or self.config.is_excluded(mac, ip):
                continue
            ip_to_mac[ip] = mac
            self._ensure_device_rules(ip)

        # Drop rules for addresses that are no longer associated with a device.
        for stale in self._tracked_ips - set(ip_to_mac):
            self._remove_device_rules(stale)
            self._prev_device_counts.pop(stale, None)

        ip_counts = self._get_iptables_counts()
        elapsed = (
            now - self._prev_device_time if self._prev_device_time is not None else 0.0
        )

        for ip, mac in ip_to_mac.items():
            counts = ip_counts.get(ip, {"rx": 0, "tx": 0})
            previous = self._prev_device_counts.get(ip)
            if previous and elapsed > 0:
                rx_rate = max(0.0, (counts["rx"] - previous[0]) / elapsed)
                tx_rate = max(0.0, (counts["tx"] - previous[1]) / elapsed)
            else:
                rx_rate = tx_rate = 0.0
            self._prev_device_counts[ip] = (counts["rx"], counts["tx"])
            self.db.record_bandwidth(
                mac=mac, ip=ip,
                rx_bytes=counts["rx"],
                tx_bytes=counts["tx"],
                rx_rate=rx_rate,
                tx_rate=tx_rate,
            )

        self._prev_device_time = now

    def cleanup_iptables(self):
        """Remove iptables chain on shutdown."""
        if not self.iptables_initialized:
            return
        try:
            chain = self.config.iptables_chain
            for hook in ["INPUT", "FORWARD"]:
                self._iptables("-D", hook, "-j", chain)
            self._iptables("-F", chain)
            self._iptables("-X", chain)
            self._tracked_ips.clear()
            self.iptables_initialized = False
            log.info("iptables accounting chain removed")
        except Exception:
            pass
