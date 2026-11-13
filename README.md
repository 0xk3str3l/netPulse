<p align="center">
  <img src="assets/banner.svg" alt="netPulse — silent LAN monitoring, self-hosted, by Skymind Automation" width="640">
</p>

<p align="center">
  <a href="https://github.com/Parrot-R/netPulse/actions/workflows/ci.yml"><img src="https://github.com/Parrot-R/netPulse/actions/workflows/ci.yml/badge.svg" alt="CI status"></a>
  <img src="https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-blue" alt="Python 3.9-3.12">
  <img src="https://img.shields.io/badge/license-MIT-informational" alt="MIT License">
  <img src="https://img.shields.io/badge/network%20egress-none-success" alt="No network egress">
</p>

**netPulse watches your own LAN, quietly, and stays on it.** Run it on a home
server or a closet Raspberry Pi and it discovers every device on the network,
tracks who's online, meters bandwidth per device, and keeps a local history —
all without a single byte leaving the network it's watching.

Think of it as a self-hosted "who's on my WiFi" + bandwidth monitor: no cloud
dashboard, no account, no phone-home telemetry. It runs as a systemd daemon,
persists to a local SQLite database, and exports snapshots to JSON/CSV when
you want to look at the data outside the live view.

---

## In the terminal

Every run opens with a small pulse-themed banner (colors degrade automatically
for `NO_COLOR`, `TERM=dumb`, or a non-interactive stream), then drops into the
live monitor if you asked for one:

```
  ▁▂▃▅▇█▇▅▃▂▁   N E T P U L S E   ▁▂▃▅▇█▇▅▃▂▁
  v1.0.0 · silent LAN monitoring, self-hosted
  ◆ Skymind Automation

========================================================================
  netPulse Live Monitor - 2026-08-19 14:02:07
  Interface: eth0  Subnet: 192.168.1.0/24
========================================================================
  Devices: 12 total | 10 online | 2 offline | 0 unknown
  Bandwidth: 812.4 KB/s RX / 96.1 KB/s TX
------------------------------------------------------------------------
  IP               MAC                Vendor           State    BW RX        BW TX
------------------------------------------------------------------------
  192.168.1.10     aa:bb:cc:11:22:33  Apple            online   214.3 KB/s   18.9 KB/s
  192.168.1.14     10:c3:7a:44:55:66  Intel            online   4.1 KB/s     1.2 KB/s
  192.168.1.22     b8:27:eb:77:88:99  Raspberry Pi     offline  0.0 B/s      0.0 B/s
  ...

  Press Ctrl+C to exit
```

## Features

- **Silent device discovery** — low-footprint ARP sweeps + MAC vendor (OUI)
  lookup, ~1,900 vendors built in
- **Adaptive presence tracking** — ICMP online/offline checks with
  exponential backoff for hosts that stay offline, so a dead device doesn't
  get pinged every few seconds forever
- **Bandwidth accounting** — per-device via `iptables`, per-interface via
  `psutil`/`/proc/net/dev`
- **Local persistence** — SQLite, with configurable retention and periodic
  pruning
- **JSON/CSV export** — on a timer while running, or on demand with
  `--export`
- **Graceful degradation** — missing root, `iptables`, or raw-socket
  capability disables just the affected subsystem and logs why; the daemon
  never crashes for it
- **A real config file** — TOML, with `CLI flags > config file > defaults`
  precedence
- **Systemd-native** — hardened unit file (`NoNewPrivileges`,
  `CapabilityBoundingSet`, `PrivateTmp`) ready to drop in

## Requirements

- Linux (uses `/proc/net/dev`, `iptables`, and AF_PACKET raw sockets)
- Python 3.9+
- Root, or `CAP_NET_RAW`/`CAP_NET_ADMIN`/`CAP_NET_BROADCAST` for full
  functionality — see [Data & privacy](#data--privacy) and
  [Graceful degradation](#features) above; netPulse still runs without them,
  just with device discovery and/or bandwidth accounting disabled
- `scapy`, `psutil`, `netifaces` (installed automatically via pip)
- `iptables` on `PATH` for per-device bandwidth accounting (optional —
  interface-level bandwidth works without it)

## Install

```bash
pip install .
```

That gives you the `netpulse` command (`console_script`, from
`pyproject.toml`). For a permanent install, drop in the systemd unit:

```bash
sudo cp packaging/netpulse.service /etc/systemd/system/
sudo mkdir -p /etc/netpulse
sudo cp packaging/netpulse.conf.example /etc/netpulse/netpulse.conf
sudo systemctl daemon-reload
sudo systemctl enable --now netpulse
```

The unit runs `netpulse --daemonize --config /etc/netpulse/netpulse.conf`
under the capability set described above — trim
`CapabilityBoundingSet`/`AmbientCapabilities` if you want to run with less
than root and let the graceful-degradation logic pick up the slack.

## Quickstart

```bash
# Foreground, auto-detected interface and subnet
sudo netpulse

# Explicit interface/subnet
sudo netpulse --interface eth0 --subnet 192.168.1.0/24

# Interactive live TUI
sudo netpulse --live

# One-shot JSON dump of a fresh discovery sweep, then exit
sudo netpulse --snapshot

# Export whatever's already in the DB, without a new sweep (safe to cron,
# and doesn't need root -- see --export below)
netpulse --export csv

# Run as a background daemon
sudo netpulse --daemonize

netpulse --version
```

## Configuration

netPulse reads `/etc/netpulse/netpulse.conf` by default (flat TOML — no
`[section]` tables). Every CLI flag overrides the file, and the file
overrides the built-in defaults below. See
[`packaging/netpulse.conf.example`](packaging/netpulse.conf.example) for a
fully commented copy of every key.

| Key | Default | What it does |
|---|---|---|
| `interface` | `"auto"` | Network interface to monitor; `auto` picks the first non-loopback interface with a default route |
| `target_subnet` | `"auto"` | Subnet CIDR to sweep; `auto` derives it from the interface's address |
| `exclude_macs` / `exclude_ips` | `[]` | Devices to never track |
| `discovery_interval` | `60` | Seconds between full ARP sweeps |
| `discovery_timeout` | `3.0` | ARP sweep timeout, seconds |
| `online_check_interval` | `10` | Ping interval for devices currently online |
| `offline_check_interval` | `120` | Starting ping interval for offline devices (doubles each miss) |
| `ping_timeout` / `ping_count` | `1.0` / `1` | Per-device ICMP check settings |
| `max_offline_backoff` | `600` | Cap on the offline ping backoff, seconds |
| `bandwidth_interval` | `5` | Seconds between bandwidth samples |
| `use_iptables` | `true` | Enable per-device accounting via `iptables` |
| `iptables_chain` | `"NETPULSE_INPUT"` | Chain name netPulse creates and manages |
| `db_path` | `/var/lib/netpulse/netpulse.db` | SQLite database location |
| `db_retention_days` | `90` | How long history is kept before pruning |
| `export_dir` | `/var/lib/netpulse/exports` | Where JSON/CSV exports land |
| `json_export_interval` / `csv_export_interval` | `300` / `3600` | Periodic export cadence while running, seconds |
| `pid_file` | `/var/run/netpulse.pid` | PID file when daemonized |
| `log_file` | `/var/log/netpulse.log` | Log file path |
| `log_level` | `"INFO"` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `daemonize` | `false` | Fork to background on start |

## Data & privacy

Everything stays on the LAN netPulse is watching. **There is no external
egress, no telemetry, no phone-home** — the only traffic netPulse generates
is ARP and ICMP on the local segment it's configured for, and that's a
guarantee, not a best effort. This is admin tooling for your own network.

## Export formats

- **JSON** (`netpulse_snapshot.json`) — full snapshot: device count, online/
  offline counts, and every known device's IP, MAC, vendor, hostname, state,
  and timestamps
- **CSV** (`netpulse_devices.csv`) — one row per device, same fields, for
  spreadsheets

Both write on a timer while the daemon runs (`json_export_interval`,
`csv_export_interval`), or on demand:

```bash
netpulse --export json
netpulse --export csv
```

`--export` reads the already-persisted database — it doesn't need root and
doesn't trigger a new ARP sweep, so it's safe to run from cron alongside a
netPulse daemon that's already collecting data.

## Uninstall

```bash
sudo systemctl disable --now netpulse
sudo rm /etc/systemd/system/netpulse.service
sudo systemctl daemon-reload
pip uninstall netpulse

# Optional -- removes all collected history
sudo rm -rf /var/lib/netpulse /var/log/netpulse.log /etc/netpulse
```

If `iptables` accounting was ever enabled, the `NETPULSE_INPUT` chain is
removed automatically on clean shutdown; nothing further to clean up there.

## License

[MIT](LICENSE) © 2026 Skymind Automation
