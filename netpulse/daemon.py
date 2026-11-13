"""Netpulse daemon: coordinates discovery, presence, bandwidth, and export."""

import logging
import os
import signal
import sys
import threading
import time
from typing import Dict, List

from .bandwidth import BandwidthMonitor
from .capabilities import check_capabilities
from .config import Config
from .db import Database
from .discovery import ARPDiscoverer, get_default_interface, get_interface_cidr, resolve_hostname
from .export import Exporter
from .presence import StateMonitor

log = logging.getLogger("netpulse")


class NetpulseDaemon:
    """Coordinator for all monitoring threads."""

    def __init__(self, config: Config):
        self.config = config
        self.running = threading.Event()
        self.running.set()

        # Resolve interface
        if config.interface == "auto":
            self.iface = get_default_interface()
        else:
            self.iface = config.interface

        # Resolve subnet
        if config.target_subnet == "auto":
            cidr = get_interface_cidr(self.iface)
            if not cidr:
                log.error("Could not determine subnet; specify --subnet")
                sys.exit(1)
            self.subnet = cidr
        else:
            self.subnet = config.target_subnet

        log.info(f"Monitoring interface: {self.iface}")
        log.info(f"Monitoring subnet: {self.subnet}")

        # Detect root / raw-socket / iptables availability up front so a
        # missing privilege disables just that subsystem instead of the
        # daemon crashing or spamming a warning every cycle.
        self.capabilities = check_capabilities(config.use_iptables)

        # Initialize components
        self.db = Database(config.db_path, config.db_retention_days)
        self.discoverer = ARPDiscoverer(
            self.iface, config.discovery_timeout,
            enabled=self.capabilities.raw_socket,
        )
        self.state_monitor = StateMonitor(self.db, config)
        self.bandwidth_monitor = BandwidthMonitor(
            self.db, config, self.iface,
            use_iptables=(config.use_iptables and self.capabilities.iptables_binary),
        )
        self.exporter = Exporter(self.db, config.export_dir)

        # Track discovered devices with hostnames
        self.devices: Dict[str, Dict] = {}
        self.last_discovery = 0
        self.last_json_export = 0
        self.last_csv_export = 0
        self.last_purge = 0

    def _resolve_hostnames(self, devices: List[Dict]):
        """Resolve hostnames for new devices (non-blocking, threaded)."""
        for d in devices:
            mac = d["mac"]
            if mac in self.devices and self.devices[mac].get("hostname"):
                d["hostname"] = self.devices[mac]["hostname"]
            else:
                name = resolve_hostname(d["ip"])
                d["hostname"] = name
                if name:
                    log.debug(f"Resolved {d['ip']} -> {name}")

    def _discovery_cycle(self):
        """Full ARP discovery sweep."""
        log.info("Running discovery sweep...")
        devices = self.discoverer.sweep(self.subnet)
        self._resolve_hostnames(devices)

        # Merge: update existing entries, add new ones, keep offline entries
        discovered_macs = {d["mac"] for d in devices}
        for d in devices:
            mac = d["mac"]
            d["state"] = self.devices.get(mac, {}).get("state", "online")
            self.devices[mac] = d

        # Mark unreachable devices that we already know about
        for mac in list(self.devices.keys()):
            if mac not in discovered_macs:
                # Don't immediately mark offline, let ping decide
                pass

        # Feed to state monitor
        self.state_monitor.update_devices(list(self.devices.values()))
        self.last_discovery = time.time()
        log.info(f"Discovery complete: {len(devices)} live devices, "
                 f"{len(self.devices)} known total")

    def _bandwidth_cycle(self):
        """Sample interface and per-device bandwidth."""
        self.bandwidth_monitor.sample_interface()
        self.bandwidth_monitor.sample_per_device(self.devices)

    def _export_cycle(self):
        """Periodic JSON/CSV export."""
        now = time.time()
        if now - self.last_json_export >= self.config.json_export_interval:
            path = self.exporter.export_json()
            log.debug(f"JSON export: {path}")
            self.last_json_export = now

        if now - self.last_csv_export >= self.config.csv_export_interval:
            path = self.exporter.export_csv()
            log.debug(f"CSV export: {path}")
            self.last_csv_export = now

    def _purge_cycle(self):
        """Periodic old data cleanup."""
        now = time.time()
        if now - self.last_purge >= 3600:  # hourly
            self.db.purge_old()
            self.last_purge = now

    def run(self):
        """Main loop. Runs discovery, state checks, and bandwidth sampling."""
        log.info("Netpulse daemon started")
        log.info(f"Devices will be checked every {self.config.online_check_interval}s "
                 f"(online) / adaptive backoff (offline)")
        log.info(f"Bandwidth sampled every {self.config.bandwidth_interval}s")
        log.info(f"Full ARP discovery every {self.config.discovery_interval}s")

        # Initial discovery
        self._discovery_cycle()

        last_bw_sample = time.time()
        last_state_check = time.time()
        last_status = time.time()

        try:
            while self.running.is_set():
                now = time.time()

                # Discovery cycle
                if now - self.last_discovery >= self.config.discovery_interval:
                    self._discovery_cycle()

                # State check cycle
                if now - last_state_check >= 1:  # Check at most once per second
                    self.state_monitor.check_all()
                    last_state_check = now

                # Bandwidth cycle
                if now - last_bw_sample >= self.config.bandwidth_interval:
                    self._bandwidth_cycle()
                    last_bw_sample = now

                # Export cycle
                self._export_cycle()

                # Purge cycle
                self._purge_cycle()

                # Periodic status log (every 5 min)
                if now - last_status >= 300:
                    online, offline = self.state_monitor.get_device_count()
                    iface_bw = self.db.get_interface_bandwidth(
                        self.iface, now - 60
                    )
                    total_rx = sum(s.get("rx_rate", 0) for s in iface_bw[-12:]) / max(len(iface_bw[-12:]), 1)
                    total_tx = sum(s.get("tx_rate", 0) for s in iface_bw[-12:]) / max(len(iface_bw[-12:]), 1)
                    log.info(
                        f"Status: {online} online, {offline} offline | "
                        f"Interface {self.iface}: "
                        f"{self._format_bw(total_rx)} RX / {self._format_bw(total_tx)} TX"
                    )
                    last_status = now

                time.sleep(0.5)  # Poll interval - prevents busy-waiting

        except KeyboardInterrupt:
            log.info("Shutdown requested")
        finally:
            self.shutdown()

    def _format_bw(self, bps: float) -> str:
        """Format bits per second to human-readable."""
        if bps < 1024:
            return f"{bps:.1f} B/s"
        elif bps < 1024 * 1024:
            return f"{bps / 1024:.1f} KB/s"
        elif bps < 1024 * 1024 * 1024:
            return f"{bps / (1024 * 1024):.1f} MB/s"
        else:
            return f"{bps / (1024 * 1024 * 1024):.2f} GB/s"

    def shutdown(self):
        """Graceful shutdown."""
        log.info("Shutting down...")
        self.bandwidth_monitor.cleanup_iptables()
        self.db.close()
        self.running.clear()
        log.info("Netpulse stopped")



    def get_snapshot(self) -> Dict:
        """Get current state as dictionary (for live queries)."""
        devices = self.db.get_devices()
        now = time.time()
        since = now - 60  # last 60 seconds
        iface_bw = self.db.get_interface_bandwidth(self.iface, since)

        avg_rx = sum(s.get("rx_rate", 0) for s in iface_bw) / max(len(iface_bw), 1)
        avg_tx = sum(s.get("tx_rate", 0) for s in iface_bw) / max(len(iface_bw), 1)

        return {
            "timestamp": now,
            "interface": self.iface,
            "subnet": self.subnet,
            "device_count": len(devices),
            "online_count": sum(1 for d in devices if d.get("state") == "online"),
            "offline_count": sum(1 for d in devices if d.get("state") == "offline"),
            "unknown_count": sum(1 for d in devices if d.get("state") == "unknown"),
            "interface_rx_rate": avg_rx,
            "interface_tx_rate": avg_tx,
            "devices": [
                {
                    "ip": d["ip"],
                    "mac": d["mac"],
                    "vendor": d.get("vendor", "Unknown"),
                    "hostname": d.get("hostname", ""),
                    "state": d.get("state", "unknown"),
                    "first_seen": d.get("first_seen", 0),
                    "last_seen": d.get("last_seen", 0),
                    "state_changed": d.get("state_changed", 0),
                }
                for d in devices
            ],
        }



# ─── Daemonization ──────────────────────────────────────────────────────────

def daemonize(pidfile: str):
    """Fork and detach into background daemon process."""
    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)  # Parent exits
    except OSError as e:
        sys.stderr.write(f"Fork failed: {e}\n")
        sys.exit(1)

    os.setsid()
    os.umask(0)

    try:
        pid = os.fork()
        if pid > 0:
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(f"Second fork failed: {e}\n")
        sys.exit(1)

    # Redirect stdio to /dev/null
    sys.stdout.flush()
    sys.stderr.flush()
    with open("/dev/null", "r") as f:
        os.dup2(f.fileno(), sys.stdin.fileno())
    with open("/dev/null", "a") as f:
        os.dup2(f.fileno(), sys.stdout.fileno())
        os.dup2(f.fileno(), sys.stderr.fileno())

    # Write PID file
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))

    # Handle PID cleanup on exit
    def cleanup(signum=None, frame=None):
        if os.path.exists(pidfile):
            os.unlink(pidfile)
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
