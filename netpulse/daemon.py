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
            mode=config.discovery_mode,
            sweep_rate=config.sweep_rate,
            active_sweep_interval=config.active_sweep_interval,
            exclude_macs=config.exclude_macs,
            exclude_ips=config.exclude_ips,
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
        self._closed = False

    def _resolve_hostnames(self, devices: List[Dict]):
        """Resolve hostnames for new devices, only when explicitly enabled.

        Reverse DNS is off by default: it is the one thing that could send
        traffic beyond the local segment, and a resolver timeout would also
        stall the discovery cycle. Opt in with ``resolve_hostnames = true``.
        """
        if not self.config.resolve_hostnames:
            for d in devices:
                d.setdefault("hostname", "")
            return
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
        """Discovery sweep: L2 ARP, OS ARP cache, or a rate-limited probe."""
        log.info("Running discovery cycle...")
        devices = [
            d for d in self.discoverer.sweep(self.subnet)
            if not self.config.is_excluded(d["mac"], d["ip"])
        ]
        self._resolve_hostnames(devices)

        # Merge: update existing entries, add new ones, keep offline entries.
        # New devices start as "unknown" until presence actually confirms them;
        # discovery alone (e.g. a stale ARP entry) is not proof of reachability.
        discovered_macs = {d["mac"] for d in devices}
        for d in devices:
            mac = d["mac"]
            d["state"] = self.devices.get(mac, {}).get("state", "unknown")
            self.devices[mac] = d

        # Feed every known device to the state monitor; in arp mode presence
        # is then resolved from this sweep, in icmp mode check_all() handles it
        # on its own schedule in run().
        self.state_monitor.update_devices(list(self.devices.values()))
        if self.config.presence_mode == "arp":
            self.state_monitor.apply_discovery_presence(list(discovered_macs))

        self.last_discovery = time.time()
        log.info(f"Discovery complete ({self.config.discovery_mode}): "
                 f"{len(devices)} live devices, {len(self.devices)} known total")

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

    def install_signal_handlers(self):
        """Turn SIGTERM/SIGINT into a clean loop exit (foreground or daemon)."""
        if threading.current_thread() is not threading.main_thread():
            return

        def _stop(signum, _frame):
            log.info(f"Signal {signum} received; shutting down")
            self.running.clear()

        for name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass

    def _safe_cycle(self, name, cycle):
        """Run one cycle; a failure must never take the whole daemon down."""
        try:
            cycle()
        except Exception as e:
            log.exception(f"{name} cycle failed (continuing): {e}")

    def run(self):
        """Main loop. Runs discovery, state checks, and bandwidth sampling."""
        self.install_signal_handlers()
        log.info("Netpulse daemon started")
        log.info(f"Presence mode: {self.config.presence_mode}; "
                 f"discovery mode: {self.config.discovery_mode}")
        log.info(f"Devices will be checked every {self.config.online_check_interval}s "
                 f"(online) / adaptive backoff (offline)")
        log.info(f"Bandwidth sampled every {self.config.bandwidth_interval}s")
        log.info(f"Full discovery every {self.config.discovery_interval}s")

        # Initial discovery
        self._safe_cycle("discovery", self._discovery_cycle)

        last_bw_sample = time.time()
        last_state_check = time.time()
        last_status = time.time()

        try:
            while self.running.is_set():
                now = time.time()

                # Discovery cycle
                if now - self.last_discovery >= self.config.discovery_interval:
                    self._safe_cycle("discovery", self._discovery_cycle)

                # State check cycle
                if now - last_state_check >= 1:  # Check at most once per second
                    self._safe_cycle("presence", self.state_monitor.check_all)
                    last_state_check = now

                # Bandwidth cycle
                if now - last_bw_sample >= self.config.bandwidth_interval:
                    self._safe_cycle("bandwidth", self._bandwidth_cycle)
                    last_bw_sample = now

                # Export cycle
                self._safe_cycle("export", self._export_cycle)

                # Purge cycle
                self._safe_cycle("purge", self._purge_cycle)

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
        """Format a byte-per-second rate to human-readable."""
        if bps < 1024:
            return f"{bps:.1f} B/s"
        elif bps < 1024 * 1024:
            return f"{bps / 1024:.1f} KB/s"
        elif bps < 1024 * 1024 * 1024:
            return f"{bps / (1024 * 1024):.1f} MB/s"
        else:
            return f"{bps / (1024 * 1024 * 1024):.2f} GB/s"

    def shutdown(self):
        """Graceful shutdown (idempotent)."""
        if self._closed:
            return
        self._closed = True
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
    """Fork and detach into background daemon process (POSIX only)."""
    if not hasattr(os, "fork"):
        sys.stderr.write(
            "--daemonize requires a POSIX system (fork). On Windows, run in "
            "the foreground or wrap netpulse with a service manager.\n"
        )
        sys.exit(1)

    if os.path.exists(pidfile):
        try:
            existing = int(open(pidfile, encoding="utf-8").read().strip() or 0)
            if existing > 0:
                os.kill(existing, 0)  # raises if the process is gone
                sys.stderr.write(
                    f"netpulse already running (pid {existing}, {pidfile})\n"
                )
                sys.exit(1)
        except (OSError, ValueError):
            pass  # stale pid file

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
    os.makedirs(os.path.dirname(pidfile) or ".", exist_ok=True)
    with open(pidfile, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

    # Remove the PID file on any clean interpreter exit; SIGTERM/SIGINT are
    # handled by NetpulseDaemon.install_signal_handlers() (installing a
    # separate signal handler here would silently replace it).
    def cleanup():
        try:
            if os.path.exists(pidfile):
                os.unlink(pidfile)
        except OSError:
            pass

    import atexit
    atexit.register(cleanup)
