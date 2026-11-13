"""Netpulse configuration: dataclass defaults, file loading, and CLI precedence.

Precedence, low to high: dataclass defaults -> config file -> CLI flags.
Config files are TOML, flat (no tables) by design, loaded with the stdlib
`tomllib` on Python 3.11+ and with a small subset parser below on 3.9/3.10 --
no third-party TOML dependency, per the ground rule of no new dependencies.
"""

import os
import sys
from dataclasses import dataclass, field, fields
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Any, Dict, List


def _default_data_dir() -> str:
    """Writable per-platform state directory (Linux keeps its FHS default)."""
    if os.name == "nt":
        base = (
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("PROGRAMDATA")
            or os.environ.get("TEMP")
            or str(Path.home())
        )
        return str(Path(base) / "netpulse")
    return "/var/lib/netpulse"


_DATA_DIR = _default_data_dir()

DEFAULT_CONFIG_PATH = (
    str(Path(_DATA_DIR) / "netpulse.conf")
    if os.name == "nt"
    else "/etc/netpulse/netpulse.conf"
)


def _normalise_mac(mac: str) -> str:
    return mac.strip().lower().replace("-", ":")


@dataclass
class Config:
    # Network
    interface: str = "auto"            # auto = first non-loopback interface
    target_subnet: str = "auto"        # auto = derived from interface CIDR
    exclude_macs: List[str] = field(default_factory=list)
    exclude_ips: List[str] = field(default_factory=list)  # IPs or CIDRs

    # Discovery
    discovery_interval: int = 60       # seconds between full ARP sweeps
    discovery_timeout: float = 3.0     # ARP timeout per sweep
    # auto: L2 ARP if available, else the OS ARP/neighbour cache plus a
    #       rate-limited ICMP sweep every active_sweep_interval.
    # passive: read the OS ARP cache only -- sends zero discovery packets.
    # active: always run a sweep.
    discovery_mode: str = "auto"
    sweep_rate: int = 32               # max ICMP probes per second while sweeping
    active_sweep_interval: int = 900   # min seconds between sweeps in auto mode

    # Online/Offline
    online_check_interval: int = 10    # seconds between checks for ONLINE devices
    offline_check_interval: int = 120  # first interval for OFFLINE devices (backoff base)
    ping_timeout: float = 1.0          # ICMP timeout per device
    ping_count: int = 1               # ICMP echo requests per check
    max_offline_backoff: int = 600     # cap backoff at 10 min
    # icmp: adaptive ping checks. arp: presence derived from discovery sweeps
    # only (zero extra traffic -- maximum stealth). auto = icmp.
    presence_mode: str = "icmp"
    ping_workers: int = 32             # parallel ICMP checks
    arp_offline_misses: int = 2        # missed sweeps before arp mode marks offline

    # Privacy: reverse-DNS lookups are opt-in and off by default so the only
    # traffic netpulse generates stays ARP/ICMP on the local segment.
    resolve_hostnames: bool = False

    # Bandwidth
    bandwidth_interval: int = 5        # seconds between bandwidth samples
    use_iptables: bool = True          # per-device via iptables (requires root)
    iptables_chain: str = "NETPULSE_INPUT"

    # Database
    db_path: str = str(Path(_DATA_DIR) / "netpulse.db")
    db_retention_days: int = 90

    # Reporting
    export_dir: str = str(Path(_DATA_DIR) / "exports")
    json_export_interval: int = 300    # 5 min
    csv_export_interval: int = 3600    # 1 hr

    # Daemon
    pid_file: str = (
        str(Path(_DATA_DIR) / "netpulse.pid")
        if os.name == "nt"
        else "/var/run/netpulse.pid"
    )
    log_file: str = (
        str(Path(_DATA_DIR) / "netpulse.log")
        if os.name == "nt"
        else "/var/log/netpulse.log"
    )
    log_level: str = "INFO"
    daemonize: bool = False

    def is_excluded(self, mac: str = "", ip: str = "") -> bool:
        """True if this device matches exclude_macs or exclude_ips (CIDRs ok)."""
        if mac:
            wanted = _normalise_mac(mac)
            if any(_normalise_mac(entry) == wanted for entry in self.exclude_macs):
                return True
        if ip:
            try:
                addr = ip_address(ip)
            except ValueError:
                return False
            for entry in self.exclude_ips:
                try:
                    if "/" in entry:
                        if addr in ip_network(entry, strict=False):
                            return True
                    elif ip_address(entry) == addr:
                        return True
                except ValueError:
                    continue
        return False

    def validate(self) -> None:
        """Reject values that would only fail later, deep inside a loop."""
        if self.discovery_mode not in ("auto", "passive", "active"):
            raise ValueError(
                f"discovery_mode must be auto, passive, or active (got {self.discovery_mode!r})"
            )
        if self.presence_mode not in ("auto", "icmp", "arp"):
            raise ValueError(
                f"presence_mode must be auto, icmp, or arp (got {self.presence_mode!r})"
            )
        for name in ("discovery_interval", "bandwidth_interval", "sweep_rate",
                     "ping_workers", "arp_offline_misses", "online_check_interval",
                     "offline_check_interval"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be greater than zero")
        if self.active_sweep_interval < 0:
            raise ValueError("active_sweep_interval must be >= 0")


_VALID_KEYS = {f.name for f in fields(Config)}


def _parse_toml_value(raw: str, lineno: int) -> Any:
    """Parse a single TOML value: string, array of strings, bool, int, or float."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_toml_value(item, lineno) for item in inner.split(",") if item.strip()]
    if value in ("true", "false"):
        return value == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    raise ValueError(f"netpulse.conf line {lineno}: cannot parse value {raw!r}")


def _strip_toml_comment(line: str) -> str:
    """Remove a trailing # comment without touching # inside a quoted string."""
    in_string = False
    escaped = False
    for i, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
        elif char == '"':
            in_string = not in_string
        elif char == "#" and not in_string:
            return line[:i]
    return line


def _parse_simple_toml(text: str) -> Dict[str, Any]:
    """Minimal TOML-subset parser for flat `key = value` files (Python < 3.11).

    Netpulse's config file has no nested tables, so this covers it without
    a third-party dependency. Supports strings, ints, floats, bools, and
    single-line arrays of strings; '#' starts a comment.
    """
    result: Dict[str, Any] = {}
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = _strip_toml_comment(raw_line).strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"netpulse.conf line {lineno}: expected 'key = value'")
        key, _, value = line.partition("=")
        result[key.strip()] = _parse_toml_value(value, lineno)
    return result


def load_config_file(path: str) -> Dict[str, Any]:
    """Load a netpulse.conf TOML file. Returns {} if the file doesn't exist."""
    file_path = Path(path)
    if not file_path.exists():
        return {}
    text = file_path.read_text(encoding="utf-8")
    if sys.version_info >= (3, 11):
        import tomllib
        return tomllib.loads(text)
    return _parse_simple_toml(text)


def apply_config_file(base: Config, file_values: Dict[str, Any]) -> Config:
    """Return a new Config with file_values layered over base's values."""
    unknown = set(file_values) - _VALID_KEYS
    if unknown:
        raise ValueError(f"Unknown netpulse.conf key(s): {', '.join(sorted(unknown))}")
    merged = {f.name: getattr(base, f.name) for f in fields(Config)}
    merged.update(file_values)
    return Config(**merged)


def apply_cli_overrides(base: Config, overrides: Dict[str, Any]) -> Config:
    """Return a new Config with non-None CLI overrides layered on top."""
    merged = {f.name: getattr(base, f.name) for f in fields(Config)}
    for key, value in overrides.items():
        if value is not None:
            merged[key] = value
    return Config(**merged)


def resolve_config(config_path: str, cli_overrides: Dict[str, Any]) -> Config:
    """Build the effective Config: defaults -> config file -> CLI flags."""
    config = apply_config_file(Config(), load_config_file(config_path))
    config = apply_cli_overrides(config, cli_overrides)
    config.validate()
    return config
