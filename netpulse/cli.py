"""Netpulse CLI: argument parsing, live TUI display, and entrypoint."""

import argparse
import json
import os
import threading
import time
from datetime import datetime

from . import __version__
from .banner import print_banner
from .config import DEFAULT_CONFIG_PATH, resolve_config
from .daemon import NetpulseDaemon, daemonize
from .db import Database
from .export import Exporter
from .logging_setup import setup_logging


def parse_args():
    parser = argparse.ArgumentParser(
        description="Netpulse - Silent Network Monitoring Daemon",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Precedence: CLI flags > --config file > built-in defaults.\n\n"
            "Examples:\n"
            "  netpulse                                   # Run in foreground\n"
            "  netpulse --daemonize                       # Run as daemon\n"
            "  netpulse --interface eth0 --subnet 10.0.0.0/24  # Custom network\n"
            "  netpulse --config /etc/netpulse/netpulse.conf   # Custom config file\n"
            "  netpulse --snapshot                        # One-shot JSON dump\n"
            "  netpulse --export csv                      # Export the stored DB to CSV and exit\n"
            "  netpulse --live                            # Interactive console\n"
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
                        help="Run as background daemon")
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
    parser.add_argument("--online-interval", type=int, default=None,
                        help="Online check interval (seconds)")
    parser.add_argument("--offline-interval", type=int, default=None,
                        help="Offline check interval base (seconds)")
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

    return parser.parse_args()


def cli_overrides_from_args(args: argparse.Namespace) -> dict:
    """Map parsed CLI args onto Config field names. Unset flags stay None."""
    no_iptables = args.no_iptables
    return {
        "interface": args.interface,
        "target_subnet": args.subnet,
        "discovery_interval": args.discovery_interval,
        "online_check_interval": args.online_interval,
        "offline_check_interval": args.offline_interval,
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


def run_live_display(daemon: NetpulseDaemon):
    """Simple live TUI using ANSI escape sequences."""
    try:
        while daemon.running.is_set():
            snap = daemon.get_snapshot()
            os.system("clear")  # or use print(\033c) for pure ANSI
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

            for dev in snap["devices"][:20]:  # Show first 20
                mac = dev["mac"]
                recent_bw = daemon.db.get_device_bandwidth(mac, time.time() - 60)
                rx_rate = sum(s.get("rx_rate", 0) for s in recent_bw) / max(len(recent_bw), 1)
                tx_rate = sum(s.get("tx_rate", 0) for s in recent_bw) / max(len(recent_bw), 1)
                print(f"  {dev['ip']:<16} {dev['mac']:<18} "
                      f"{dev['vendor'][:16]:<16} {dev['state']:<8} "
                      f"{daemon._format_bw(rx_rate):<12} {daemon._format_bw(tx_rate):<12}")

            print("\n  Press Ctrl+C to exit")
            time.sleep(2)

    except KeyboardInterrupt:
        pass


def main():
    args = parse_args()

    # --export is meant for cron/scripts -- keep its output machine-clean.
    if not args.export:
        print_banner(__version__)

    # Build config: dataclass defaults -> --config file -> CLI flags
    try:
        config = resolve_config(args.config, cli_overrides_from_args(args))
    except (ValueError, OSError) as e:
        print(f"[!] Config error: {e}")
        raise SystemExit(1)

    # Setup logging
    setup_logging(config.log_file, config.log_level)

    # One-shot export of the stored DB -- doesn't touch the network, so it
    # runs before daemon setup (interface/subnet resolution) altogether.
    if args.export:
        db = Database(config.db_path, config.db_retention_days)
        exporter = Exporter(db, config.export_dir)
        path = exporter.export_json() if args.export == "json" else exporter.export_csv()
        print(f"Exported to {path}")
        db.close()
        return

    # Daemonize if requested
    if config.daemonize:
        daemonize(config.pid_file)

    # Create and run daemon
    daemon = NetpulseDaemon(config)

    if args.snapshot:
        daemon._discovery_cycle()
        time.sleep(5)  # Let initial checks settle
        snap = daemon.get_snapshot()
        print(json.dumps(snap, indent=2, default=str))
        daemon.shutdown()
        return

    if args.live:
        # Start daemon in background thread, show live TUI
        dt = threading.Thread(target=daemon.run, daemon=True)
        dt.start()
        time.sleep(2)
        run_live_display(daemon)
        daemon.running.clear()
        daemon.shutdown()
        return

    # Run in foreground
    daemon.run()


if __name__ == "__main__":
    main()
