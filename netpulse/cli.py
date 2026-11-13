"""Netpulse CLI: argument parsing, live TUI display, and entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

from . import __version__
from .banner import print_banner
from .config import DEFAULT_CONFIG_PATH, resolve_config
from .db import Database
from .export import Exporter
from .logging_setup import setup_logging

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids importing scapy
    from .daemon import NetpulseDaemon


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Netpulse - Silent Network Monitoring Daemon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Precedence: CLI flags > --config file > built-in defaults.\n\n"
            "Examples:\n"
            "  netpulse                                   # Run in foreground\n"
            "  netpulse --daemonize                       # Run as daemon (POSIX)\n"
            "  netpulse --interface eth0 --subnet 10.0.0.0/24  # Custom network\n"
            "  netpulse --config /etc/netpulse/netpulse.conf   # Custom config file\n"
            "  netpulse --snapshot                        # One-shot JSON dump\n"
            "  netpulse --export csv                      # Export the stored DB to CSV and exit\n"
            "  netpulse --live                            # Interactive console\n\n"
            "Stealth presets:\n"
            "  netpulse --discovery-mode passive --presence-mode arp\n"
            "      # zero discovery probes, presence from the ARP cache only\n"
            "  netpulse --discovery-mode active --active-sweep-interval 300\n"
            "      # rate-limited ICMP sweep every 5 min, no per-host pings\n"
        ),
    )

    parser.add_argument("--version", action="version",
                        version=f"netpulse {__version__}")

    # Unset (None) defaults on everything that maps to a Config field, so
    # resolve_config() can tell "not passed on the CLI" apart from "passed
    # with this value" and apply the file -> CLI precedence correctly.
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help=f"Config file path (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("-i", "--interface", default=None,
                        help="Network interface (default: auto-detect)")
    parser.add_argument("-s", "--subnet", default=None,
                        help="Target subnet CIDR (default: auto)")
    parser.add_argument("-d", "--daemonize", action="store_true", default=None,
                        help="Run as background daemon (POSIX only)")
    parser.add_argument("--db", default=None,
                        help="Database path")
    parser.add_argument("--log", default=None,
                        help="Log file path")
    parser.add_argument("--log-level", default=None,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging level")
    parser.add_argument("--snapshot", action="store_true",
                        help="Run one discovery sweep, print a JSON snapshot, and exit")
    parser.add_argument("--export", choices=["json", "csv"], default=None,
                        help="Export the stored device DB (no new sweep) and exit")
    parser.add_argument("--live", action="store_true",
                        help="Interactive live TUI display")
    parser.add_argument("--discovery-interval", type=int, default=None,
                        help="ARP discovery interval (seconds)")
    parser.add_argument("--discovery-mode", default=None,
                        choices=["auto", "passive", "active"],
                        help="auto=L2 ARP or OS cache + light sweeps; "
                             "passive=OS ARP cache only (zero packets); "
                             "active=always sweep")
    parser.add_argument("--sweep-rate", type=int, default=None,
                        help="Max ICMP probes per second during an active sweep")
    parser.add_argument("--active-sweep-interval", type=int, default=None,
                        help="Min seconds between active sweeps in auto mode")
    parser.add_argument("--online-interval", type=int, default=None,
                        help="Online check interval (seconds)")
    parser.add_argument("--offline-interval", type=int, default=None,
                        help="First offline check interval (backoff base, seconds)")
    parser.add_argument("--presence-mode", default=None,
                        choices=["auto", "icmp", "arp"],
                        help="icmp=adaptive pings; arp=derive presence from "
                             "discovery sweeps only (zero traffic)")
    parser.add_argument("--ping-workers", type=int, default=None,
                        help="Parallel ICMP checks (default: 32)")
    parser.add_argument("--arp-offline-misses", type=int, default=None,
                        help="Missed sweeps before arp-mode marks a device offline")
    parser.add_argument("--resolve-hostnames", action="store_true", default=None,
                        help="Enable reverse-DNS lookups (off by default: no egress)")
    parser.add_argument("--bw-interval", type=int, default=None,
                        help="Bandwidth sample interval (seconds)")
    parser.add_argument("--no-iptables", action="store_true", default=None,
                        help="Disable per-device iptables accounting")
    parser.add_argument("--export-dir", default=None,
                        help="Export directory for JSON/CSV")
    parser.add_argument("--retention", type=int, default=None,
                        help="Days to retain bandwidth history")
    parser.add_argument("--pidfile", default=None,
                        help="PID file path")

    args = parser.parse_args(argv)
    if args.snapshot and args.live:
        parser.error("--snapshot and --live are mutually exclusive")
    return args


def cli_overrides_from_args(args: argparse.Namespace) -> dict:
    """Map parsed CLI args onto Config field names. Unset flags stay None."""
    no_iptables = args.no_iptables
    return {
        "interface": args.interface,
        "target_subnet": args.subnet,
        "discovery_interval": args.discovery_interval,
        "discovery_mode": args.discovery_mode,
        "sweep_rate": args.sweep_rate,
        "active_sweep_interval": args.active_sweep_interval,
        "online_check_interval": args.online_interval,
        "offline_check_interval": args.offline_interval,
        "presence_mode": args.presence_mode,
        "ping_workers": args.ping_workers,
        "arp_offline_misses": args.arp_offline_misses,
        "resolve_hostnames": args.resolve_hostnames,
        "bandwidth_interval": args.bw_interval,
        "use_iptables": None if no_iptables is None else (not no_iptables),
        "db_path": args.db,
        "log_file": args.log,
        "log_level": args.log_level,
        "export_dir": args.export_dir,
        "db_retention_days": args.retention,
        "pid_file": args.pidfile,
        "daemonize": args.daemonize,
    }


def _clear_screen() -> None:
    if not sys.stdout.isatty():
        return
    os.system("cls" if os.name == "nt" else "clear")


def run_live_display(daemon: "NetpulseDaemon"):
    """Simple live TUI using ANSI escape sequences."""
    try:
        while daemon.running.is_set():
            try:
                snap = daemon.get_snapshot()
            except Exception:
                time.sleep(1)
                continue

            _clear_screen()
            print("=" * 72)
            print(f"  Netpulse Live Monitor - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"  Interface: {snap['interface']}  Subnet: {snap['subnet']}")
            print("=" * 72)
            print(f"  Devices: {snap['device_count']} total | "
                  f"{snap['online_count']} online | "
                  f"{snap['offline_count']} offline | "
                  f"{snap['unknown_count']} unknown")
            print(f"  Bandwidth: {daemon._format_bw(snap['interface_rx_rate'])} RX / "
                  f"{daemon._format_bw(snap['interface_tx_rate'])} TX")
            print("-" * 72)
            print(f"  {'IP':<16} {'MAC':<18} {'Vendor':<16} {'State':<8} {'BW RX':<12} {'BW TX':<12}")
            print("-" * 72)

            devices = snap["devices"]
            for dev in devices[:40]:
                mac = dev["mac"]
                recent_bw = daemon.db.get_device_bandwidth(mac, time.time() - 60)
                rx_rate = sum(s.get("rx_rate", 0) for s in recent_bw) / max(len(recent_bw), 1)
                tx_rate = sum(s.get("tx_rate", 0) for s in recent_bw) / max(len(recent_bw), 1)
                print(f"  {dev['ip']:<16} {dev['mac']:<18} "
                      f"{dev['vendor'][:16]:<16} {dev['state']:<8} "
                      f"{daemon._format_bw(rx_rate):<12} {daemon._format_bw(tx_rate):<12}")
            if len(devices) > 40:
                print(f"  ... and {len(devices) - 40} more")

            print("\n  Press Ctrl+C to exit")
            time.sleep(2)

    except KeyboardInterrupt:
        pass


def _export_and_exit(args, config) -> None:
    """Export the persisted DB without root, logging setup, or a new sweep."""
    try:
        db = Database(config.db_path, config.db_retention_days, read_only=True)
    except (FileNotFoundError, OSError, sqlite3.Error) as e:
        print(f"[!] Export failed: {e}")
        raise SystemExit(1)
    try:
        exporter = Exporter(db, config.export_dir)
        path = exporter.export_json() if args.export == "json" else exporter.export_csv()
        print(f"Exported to {path}")
    except (OSError, sqlite3.Error) as e:
        print(f"[!] Export failed: {e}")
        raise SystemExit(1)
    finally:
        db.close()


def main(argv=None):
    args = parse_args(argv)

    # --export is meant for cron/scripts -- keep its output machine-clean and
    # skip logging/file setup entirely (it must work unprivileged).
    if not args.export:
        print_banner(__version__)

    # Build config: dataclass defaults -> --config file -> CLI flags
    try:
        config = resolve_config(args.config, cli_overrides_from_args(args))
    except (ValueError, OSError) as e:
        print(f"[!] Config error: {e}")
        raise SystemExit(1)

    if args.export:
        _export_and_exit(args, config)
        return

    if (args.snapshot or args.live) and config.daemonize:
        print("[!] --snapshot/--live cannot be combined with daemonize")
        raise SystemExit(1)

    setup_logging(config.log_file, config.log_level)

    # Imported here (not at module import) so `--export` doesn't drag in
    # scapy/netifaces/psutil -- keeping it fast and quiet for cron.
    from .daemon import NetpulseDaemon, daemonize

    if config.daemonize:
        daemonize(config.pid_file)

    daemon = NetpulseDaemon(config)

    if args.snapshot:
        daemon._discovery_cycle()
        # Presence pass, so the snapshot reflects real reachability instead of
        # discovery's optimistic default. In arp mode the sweep already did it.
        daemon.state_monitor.check_all()
        print(json.dumps(daemon.get_snapshot(), indent=2, default=str))
        daemon.shutdown()
        return

    if args.live:
        # Start the daemon in a background thread, show the live TUI.
        daemon.install_signal_handlers()
        dt = threading.Thread(target=daemon.run, daemon=True)
        dt.start()
        try:
            run_live_display(daemon)
        finally:
            daemon.running.clear()
            dt.join(timeout=10)
            daemon.shutdown()
        return

    # Run in foreground
    daemon.run()


if __name__ == "__main__":
    main()
