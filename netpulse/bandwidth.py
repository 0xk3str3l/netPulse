"""Bandwidth tracking: interface-level via psutil, per-device via iptables."""

import logging
import subprocess
import time
from typing import Dict, Optional, Tuple

import psutil

from .config import Config
from .db import Database

log = logging.getLogger("netpulse")


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
        effective_use_iptables = config.use_iptables if use_iptables is None else use_iptables
        if effective_use_iptables:
            self._init_iptables()

    def _init_iptables(self):
        """Create iptables chain for per-IP accounting."""
        try:
            chain = self.config.iptables_chain
            # Create chain
            created = subprocess.run(
                ["iptables", "-N", chain],
                capture_output=True, text=True, check=False
            )
            if created.returncode != 0 and "Chain already exists" not in created.stderr:
                log.warning(
                    f"iptables chain creation failed ({created.stderr.strip() or 'permission denied?'}); "
                    "per-device bandwidth accounting disabled (needs root)"
                )
                return

            # Add to INPUT and FORWARD if not already there
            for hook in ["INPUT", "FORWARD"]:
                existing = subprocess.run(
                    ["iptables", "-C", hook, "-j", chain],
                    capture_output=True, text=True, check=False
                )
                if existing.returncode != 0:
                    inserted = subprocess.run(
                        ["iptables", "-I", hook, "-j", chain],
                        capture_output=True, text=True, check=False
                    )
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

    def _get_iptables_counts(self) -> Dict[str, Dict[str, int]]:
        """Read per-IP byte counts from iptables accounting chain."""
        result = {}
        if not self.iptables_initialized:
            return result
        try:
            chain = self.config.iptables_chain
            output = subprocess.run(
                ["iptables", "-L", chain, "-n", "-v", "-x"],
                capture_output=True, text=True, check=True
            ).stdout

            for line in output.splitlines():
                # Parse: pkts bytes target prot opt in out source destination
                parts = line.strip().split()
                if len(parts) >= 8 and parts[2] == "ACCEPT":
                    try:
                        bytes_val = int(parts[1])
                        src = parts[7]
                        dst = parts[8]
                        if src not in result:
                            result[src] = {"rx": 0, "tx": 0}
                        if dst not in result:
                            result[dst] = {"rx": 0, "tx": 0}
                        result[src]["tx"] += bytes_val
                        result[dst]["rx"] += bytes_val
                    except (ValueError, IndexError):
                        continue
        except Exception as e:
            log.warning(f"iptables read failed: {e}")
        return result

    def sample_interface(self):
        """Sample interface-level bandwidth and store to DB."""
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
            rx_rate = (rx_bytes - prev_rx) / elapsed
            tx_rate = (tx_bytes - prev_tx) / elapsed

            self.db.record_interface_bw(self.iface, rx_bytes, tx_bytes, rx_rate, tx_rate)

            self.prev_iface_counters[self.iface] = (rx_bytes, tx_bytes)
            self.prev_time = now

        except Exception as e:
            log.warning(f"Interface bandwidth sample failed: {e}")

    def sample_per_device(self, devices: Dict[str, Dict]):
        """Map iptables byte counts to known devices and store."""
        if not self.iptables_initialized:
            return

        ip_counts = self._get_iptables_counts()

        for mac, device in devices.items():
            ip = device.get("ip")
            if ip and ip in ip_counts:
                counts = ip_counts[ip]
                self.db.record_bandwidth(
                    mac=mac, ip=ip,
                    rx_bytes=counts["rx"],
                    tx_bytes=counts["tx"],
                    rx_rate=0,  # Requires differential; stored raw
                    tx_rate=0
                )

    def cleanup_iptables(self):
        """Remove iptables chain on shutdown."""
        if not self.iptables_initialized:
            return
        try:
            chain = self.config.iptables_chain
            for hook in ["INPUT", "FORWARD"]:
                subprocess.run(
                    ["iptables", "-D", hook, "-j", chain],
                    capture_output=True, check=False
                )
            subprocess.run(["iptables", "-F", chain], capture_output=True, check=False)
            subprocess.run(["iptables", "-X", chain], capture_output=True, check=False)
            log.info("iptables accounting chain removed")
        except Exception:
            pass
