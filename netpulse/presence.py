"""Adaptive ICMP online/offline presence detection with exponential backoff."""

import logging
import subprocess
import threading
import time
from typing import Dict, List, Tuple

from .config import Config
from .db import Database

log = logging.getLogger("netpulse")


class StateMonitor:
    """Adaptive ping-based online/offline detection with exponential backoff."""

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.devices: Dict[str, Dict] = {}  # mac -> device info
        self.last_checks: Dict[str, float] = {}
        self.backoff: Dict[str, int] = {}    # mac -> current interval
        self.lock = threading.Lock()

    def update_devices(self, devices: List[Dict]):
        with self.lock:
            for d in devices:
                mac = d["mac"]
                if mac not in self.devices:
                    self.backoff[mac] = self.config.online_check_interval
                self.devices[mac] = d
                # Ensure device record exists
                self.db.upsert_device(
                    mac=d["mac"], ip=d["ip"],
                    vendor=d.get("vendor", "Unknown"),
                    state=self.devices[mac].get("state", "unknown")
                )

    def _ping(self, ip: str) -> bool:
        """Single silent ICMP check. Returns True if reachable."""
        try:
            # Use system ping for low overhead
            result = subprocess.run(
                ["ping", "-c", str(self.config.ping_count),
                 "-W", str(int(self.config.ping_timeout)),
                 "-q", "-n", ip],
                capture_output=True,
                text=True,
                timeout=self.config.ping_timeout + 0.5
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
            return False

    def check_device(self, mac: str) -> bool:
        """Check a single device's online status."""
        device = self.devices.get(mac)
        if not device:
            return False
        return self._ping(device["ip"])

    def check_all(self):
        """Check all known devices with adaptive intervals."""
        with self.lock:
            now = time.time()
            to_check = []

            for mac, device in list(self.devices.items()):
                interval = self.backoff.get(mac, self.config.online_check_interval)
                last = self.last_checks.get(mac, 0)
                if now - last >= interval:
                    to_check.append(mac)

        for mac in to_check:
            try:
                device = self.devices.get(mac)
                if not device:
                    continue

                is_online = self._ping(device["ip"])
                old_state = device.get("state", "unknown")
                new_state = "online" if is_online else "offline"

                with self.lock:
                    device["state"] = new_state
                    self.last_checks[mac] = time.time()

                self.db.upsert_device(
                    mac=device["mac"], ip=device["ip"],
                    hostname=device.get("hostname", ""),
                    vendor=device.get("vendor", "Unknown"),
                    state=new_state
                )

                if new_state == "online":
                    self.backoff[mac] = self.config.online_check_interval
                else:
                    # Exponential backoff for offline devices
                    current = self.backoff.get(mac, self.config.offline_check_interval)
                    self.backoff[mac] = min(
                        current * 2,
                        self.config.max_offline_backoff
                    )

                if old_state != new_state:
                    log.info(f"Device {device['ip']} ({device.get('vendor', '?')}) "
                             f"state changed: {old_state} -> {new_state}")

            except Exception as e:
                log.warning(f"Error checking {mac}: {e}")

    def get_device_count(self) -> Tuple[int, int]:
        with self.lock:
            online = sum(1 for d in self.devices.values() if d.get("state") == "online")
            offline = sum(1 for d in self.devices.values() if d.get("state") == "offline")
            return online, offline
