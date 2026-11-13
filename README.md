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

Linux is the primary, fully supported platform. **Windows 10/11 and other
non-Linux hosts are supported on a best-effort basis** for discovery, presence,
and interface-level bandwidth — see [Platform support](#platform-support).

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

- **Silent device discovery** — L2 ARP sweeps when raw sockets are available,
  otherwise the OS ARP/neighbour cache (zero packets to read) plus a
  rate-limited ICMP sweep; MAC vendor (OUI) lookup with ~1,900 vendors built in
- **Stealth controls** — `discovery_mode` and `presence_mode` let you dial
  discovery and presence down to *zero* emitted packets, and `sweep_rate` caps
  an active sweep at a boring, admin-looking pace
- **Adaptive presence tracking** — parallel ICMP online/offline checks with
  exponential backoff for hosts that stay offline, so a dead device doesn't
  get pinged every few seconds forever
- **Bandwidth accounting** — per-device via `iptables` (Linux + root),
  per-interface via `psutil`
- **Local persistence** — SQLite (WAL), with configurable retention and
  periodic pruning
- **JSON/CSV export** — on a timer while running, or on demand with
  `--export`, which reads the DB read-only and needs no privileges
- **Graceful degradation** — a missing root, `iptables`, Npcap, or raw-socket
  capability disables just the affected subsystem and logs why; the daemon
  never crashes for it
- **A real config file** — TOML, with `CLI flags > config file > defaults`
  precedence
- **Systemd-native** — hardened unit file (`NoNewPrivileges`,
  `CapabilityBoundingSet`, `PrivateTmp`) ready to drop in

## Requirements

- Linux for the full feature set (uses `iptables` and AF_PACKET raw sockets).
  Windows 10/11, macOS, and other POSIX hosts work for discovery, presence,
  and interface-level bandwidth — see [Platform support](#platform-support)
- Python 3.9+
- Root, or `CAP_NET_RAW`/`CAP_NET_ADMIN`/`CAP_NET_BROADCAST` for full
  functionality — see [Data & privacy](#data--privacy) and
  [Graceful degradation](#features) above; netPulse still runs without them,
  just with fewer discovery options and/or no per-device bandwidth accounting
- `scapy`, `psutil`, `netifaces` (installed automatically via pip)
- `iptables` on `PATH` for per-device bandwidth accounting (Linux, optional —
  interface-level bandwidth works without it)
- On Windows, [Npcap](https://npcap.com/) only if you want L2 ARP sweeps;
  discovery otherwise uses the OS ARP table

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

# Run as a background daemon (POSIX only)
sudo netpulse --daemonize

netpulse --version
```

On Windows, drop the `sudo` prefix and name the adapter as Windows does.
Everything except L2 ARP sweeps (which need Npcap *and* Administrator) works
unprivileged from a normal PowerShell:

```powershell
netpulse --snapshot --interface "Ethernet 2" --subnet 192.168.2.0/24
```

## Stealth

netPulse can be as loud or as quiet as you want. The two knobs that matter are
`discovery_mode` and `presence_mode`:

| Preset | Flags | Traffic |
|---|---|---|
| **Zero-packet** | `--discovery-mode passive --presence-mode arp` | No discovery probes and no presence pings. Devices come from the OS ARP cache; online/offline is derived from sweeps (`arp_offline_misses`). Resolution is bounded by `discovery_interval`. |
| **Quiet** | `--discovery-mode auto` (default) | L2 ARP when possible; otherwise the OS ARP cache plus one rate-limited ICMP sweep at most every `active_sweep_interval` (default 15 min, `sweep_rate` 32/s). |
| **Active** | `--discovery-mode active --active-sweep-interval 300` | A sweep every 5 minutes, regardless of cache warmth. Still rate-limited. |

In every mode the only packets netPulse emits are local ARP/ICMP on the
configured subnet. Reverse DNS is **off by default**; enable it with
`--resolve-hostnames` (or `resolve_hostnames = true`) only if you accept
resolver traffic.

## Configuration

netPulse reads `/etc/netpulse/netpulse.conf` by default on Linux
(`%LOCALAPPDATA%\netpulse\netpulse.conf` on Windows). Flat TOML — no
`[section]` tables. Every CLI flag overrides the file, and the file overrides
the built-in defaults below. See
[`packaging/netpulse.conf.example`](packaging/netpulse.conf.example) for a
fully commented copy of every key.

| Key | Default | What it does |
|---|---|---|
| `interface` | `"auto"` | Network interface to monitor; `auto` picks the first non-loopback interface with a default route. On Windows this is the friendly name (e.g. `"Ethernet 2"`) |
| `target_subnet` | `"auto"` | Subnet CIDR to sweep; `auto` derives it from the interface's address |
| `exclude_macs` / `exclude_ips` | `[]` | Devices to never track. MACs are case/separator insensitive; IPs may be plain addresses or CIDRs |
| `discovery_interval` | `60` | Seconds between discovery cycles |
| `discovery_timeout` | `3.0` | ARP sweep timeout, seconds |
| `discovery_mode` | `"auto"` | `auto` = L2 ARP if available, else OS ARP cache + a sweep every `active_sweep_interval`; `passive` = OS ARP cache only (zero packets); `active` = always sweep |
| `sweep_rate` | `32` | Max ICMP probes per second during an active sweep |
| `active_sweep_interval` | `900` | Minimum seconds between active sweeps in `auto` mode |
| `online_check_interval` | `10` | Ping interval for devices currently online |
| `offline_check_interval` | `120` | First ping interval for a device that just went offline; doubles on each further miss |
| `ping_timeout` / `ping_count` | `1.0` / `1` | Per-device ICMP check settings |
| `max_offline_backoff` | `600` | Cap on the offline ping backoff, seconds |
| `presence_mode` | `"icmp"` | `icmp` = adaptive parallel pings; `arp` = derive presence from discovery sweeps only (zero extra traffic); `auto` = `icmp` |
| `ping_workers` | `32` | Parallel ICMP checks |
| `arp_offline_misses` | `2` | Consecutive missed sweeps before `arp` mode marks a device offline |
| `resolve_hostnames` | `false` | Reverse-DNS lookups. Off by default so no traffic leaves the local segment |
| `bandwidth_interval` | `5` | Seconds between bandwidth samples |
| `use_iptables` | `true` | Enable per-device accounting via `iptables` (Linux + root; ignored where unavailable) |
| `iptables_chain` | `"NETPULSE_INPUT"` | Chain name netPulse creates and manages |
| `db_path` | `/var/lib/netpulse/netpulse.db` | SQLite database location (`%LOCALAPPDATA%\netpulse\netpulse.db` on Windows) |
| `db_retention_days` | `90` | How long history is kept before pruning |
| `export_dir` | `/var/lib/netpulse/exports` | Where JSON/CSV exports land |
| `json_export_interval` / `csv_export_interval` | `300` / `3600` | Periodic export cadence while running, seconds |
| `pid_file` | `/var/run/netpulse.pid` | PID file when daemonized |
| `log_file` | `/var/log/netpulse.log` | Log file path (rotated at 5 MB, 2 backups) |
| `log_level` | `"INFO"` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `daemonize` | `false` | Fork to background on start (POSIX only) |

## Data & privacy

Everything stays on the LAN netPulse is watching. **There is no external
egress, no telemetry, no phone-home.** The only traffic netPulse generates is
ARP and ICMP on the local segment it's configured for — plus reverse-DNS
lookups only if you opt in with `resolve_hostnames`. With the default
settings and the zero-packet preset, netPulse can run without emitting any
packets of its own at all. This is admin tooling for your own network.

## Platform support

| Capability | Linux | Windows 10/11 | Notes |
|---|---|---|---|
| Device discovery | L2 ARP or OS cache | OS ARP table (`arp -a`) + rate-limited ICMP; L2 ARP only with Npcap + Administrator | `discovery_mode` applies on both |
| Presence tracking | ICMP or ARP-derived | ICMP or ARP-derived | Platform-correct `ping` flags |
| Interface bandwidth | `psutil` | `psutil` | True byte-rate per interface |
| Per-device bandwidth | `iptables` (root) | **Not available** | Windows has no `iptables`; device rules are skipped, interface totals still recorded |
| `--daemonize` | yes (`fork`) | no | Run foreground or wrap in a service manager |
| `--export json/csv` | yes | yes | Reads the DB read-only; no new traffic, no elevation needed |

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

`--export` opens the already-persisted database in read-only mode — it
doesn't need root/Administrator and doesn't trigger a new ARP sweep, so it's
safe to run from cron alongside a netPulse daemon that's already collecting
data. It still needs *filesystem* read access to the DB file (and its WAL),
and write access to `export_dir`; a root-owned `0600` database remains
unreadable to an unprivileged user, by design.

## Uninstall

```bash
sudo systemctl disable --now netpulse
sudo rm /etc/systemd/system/netpulse.service
sudo systemctl daemon-reload
pip uninstall netpulse

# Optional -- removes all collected history
sudo rm -rf /var/lib/netpulse /var/log/netpulse.log /etc/netpulse
```

On Windows the state lives under `%LOCALAPPDATA%\netpulse`; delete that folder
after stopping the process to remove the database, logs, exports, and PID
file.

If `iptables` accounting was ever enabled, the `NETPULSE_INPUT` chain is
removed automatically on clean shutdown; nothing further to clean up there.

## License

[MIT](LICENSE) © 2026 Skymind Automation
