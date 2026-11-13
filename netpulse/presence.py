"""Adaptive ICMP online/offline presence detection with exponential backoff.

Two modes:

* ``icmp`` (default): parallel, adaptive ping checks. Offline devices back off
  from ``offline_check_interval`` up to ``max_offline_backoff`` so a dead host
  is not probed forever.
* ``arp``: no extra traffic at all. Presence is derived from the discovery
  sweep -- seen in the ARP table means online, missing for
  ``arp_offline_misses`` sweeps means offline. Maximum stealth; resolution is
  bounded by ``discovery_interval``.
"""

import logging
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from .config import Config
from .db import Database
from .discovery import ping_command

log = logging.getLogger("netpulse")


class StateMonitor:
    """Adaptive ping-based (or fully passive ARP-based) presence tracking."""

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.devices: Dict[str, Dict] = {}  # mac -> device info
        self.last_checks: Dict[str, float] = {}
        self.backoff: Dict[str, int] = {}    # mac -> current interval
        self.misses: Dict[str, int] = {}     # arp mode: consecutive missed sweeps
        self.lock = threading.Lock()

    def update_devices(self, devices: List[Dict]):
        new_rows = []
        with self.lock:
            for d in devices:
                mac = d["mac"]
                if self.config.is_excluded(mac, d.get("ip", "")):
                    continue
                if mac not in self.devices:
                    self.backoff[mac] = self.config.online_check_interval
                    self.misses[mac] = 0
                    new_rows.append({
                        "mac": mac, "ip": d.get("ip", ""),
                        "hostname": d.get("hostname", ""),
                        "vendor": d.get("vendor", "Unknown"),
                        "state": d.get("state", "unknown"),
                    })
                self.devices[mac] = d
        # Only insert devices we have not seen before. Presence updates
        # last_seen for devices actually observed; re-upserting everything on
        # every sweep would refresh last_seen for hosts that never answered.
        if new_rows:
            self.db.upsert_devices(new_rows)

    def _ping(self, ip: str) -> bool:
        """Single silent ICMP check. Returns True if reachable."""
        try:
            result = subprocess.run(
                ping_command(
                    ip,
                    count=self.config.ping_count,
                    timeout=self.config.ping_timeout,
                ),
                capture_output=True,
                text=True,
                timeout=self.config.ping_timeout + 1.0,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    def check_device(self, mac: str) -> bool:
        """Check a single device's online status."""
        device = self.devices.get(mac)
        if not device:
            return False
        return self._ping(device["ip"])

    def _check_mac(self, mac: str) -> Tuple[str, Optional[bool]]:
        device = self.devices.get(mac)
        if not device:
            return mac, None
        if self.config.is_excluded(mac, device.get("ip", "")):
            return mac, None
        try:
            return mac, self._ping(device["ip"])
        except Exception as e:  # never let one host kill the cycle
            log.warning(f"Error checking {mac}: {e}")
            return mac, None

    def check_all(self):
        """Check all due devices, in parallel, with adaptive intervals."""
        if self.config.presence_mode == "arp":
            return

        now = time.time()
        with self.lock:
            due = [
                mac for mac, device in self.devices.items()
                if now - self.last_checks.get(mac, 0)
                >= self.backoff.get(mac, self.config.online_check_interval)
                and not self.config.is_excluded(mac, device.get("ip", ""))
            ]

        if not due:
            return

        workers = max(1, min(self.config.ping_workers, len(due)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(self._check_mac, due))

        rows = []
        for mac, is_online in results:
            if is_online is not None:
                row = self._apply_state(mac, is_online)
                if row:
                    rows.append(row)
        if rows:
            self.db.upsert_devices(rows)

    def _apply_state(self, mac: str, is_online: bool):
        with self.lock:
            device = self.devices.get(mac)
            if device is None:
                return None
            old_state = device.get("state", "unknown")
            new_state = "online" if is_online else "offline"
            device["state"] = new_state
            self.last_checks[mac] = time.time()

            if new_state == "online":
                self.backoff[mac] = self.config.online_check_interval
            elif old_state != "offline":
                # First miss: start at the configured offline interval rather
                # than doubling all the way up from the online cadence.
                self.backoff[mac] = self.config.offline_check_interval
            else:
                current = self.backoff.get(mac, self.config.offline_check_interval)
                self.backoff[mac] = min(
                    current * 2, self.config.max_offline_backoff
                )
            snapshot = dict(device)

        if old_state != new_state:
            log.info(f"Device {snapshot['ip']} ({snapshot.get('vendor', '?')}) "
                     f"state changed: {old_state} -> {new_state}")

        return {
            "mac": snapshot["mac"], "ip": snapshot["ip"],
            "hostname": snapshot.get("hostname", ""),
            "vendor": snapshot.get("vendor", "Unknown"),
            "state": new_state,
        }

    def apply_discovery_presence(self, seen_macs: List[str]):
        """Fully passive presence for ``presence_mode = arp``.

        Called once per discovery sweep. Devices seen in the ARP table are
        online; devices missing for ``arp_offline_misses`` consecutive sweeps
        are offline. Emits no packets of its own.
        """
        if self.config.presence_mode != "arp":
            return

        seen = set(seen_macs)
        rows = []
        changes = []
        now = time.time()
        with self.lock:
            for mac, device in self.devices.items():
                if self.config.is_excluded(mac, device.get("ip", "")):
                    continue
                if mac in seen:
                    self.misses[mac] = 0
                    new_state = "online"
                else:
                    misses = self.misses.get(mac, 0) + 1
                    self.misses[mac] = misses
                    if misses < max(1, self.config.arp_offline_misses):
                        continue
                    new_state = "offline"

                old_state = device.get("state", "unknown")
                device["state"] = new_state
                self.last_checks[mac] = now
                if old_state != new_state or mac in seen:
                    rows.append({
                        "mac": device["mac"], "ip": device.get("ip", ""),
                        "hostname": device.get("hostname", ""),
                        "vendor": device.get("vendor", "Unknown"),
                        "state": new_state,
                    })
                if old_state != new_state:
                    changes.append((device.get("ip", "?"),
                                    device.get("vendor", "?"),
                                    old_state, new_state))

        if rows:
            self.db.upsert_devices(rows)
        for ip, vendor, old_state, new_state in changes:
            log.info(f"Device {ip} ({vendor}) state changed: {old_state} -> {new_state}")

    def get_device_count(self) -> Tuple[int, int]:
        with self.lock:
            online = sum(1 for d in self.devices.values() if d.get("state") == "online")
            offline = sum(1 for d in self.devices.values() if d.get("state") == "offline")
            return online, offline
