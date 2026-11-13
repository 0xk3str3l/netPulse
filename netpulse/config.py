"""Netpulse configuration: dataclass defaults, file loading, and CLI precedence.

Precedence, low to high: dataclass defaults -> config file -> CLI flags.
Config files are TOML, flat (no tables) by design, loaded with the stdlib
`tomllib` on Python 3.11+ and with a small subset parser below on 3.9/3.10 --
no third-party TOML dependency, per the ground rule of no new dependencies.
"""

import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_CONFIG_PATH = "/etc/netpulse/netpulse.conf"


@dataclass
class Config:
    # Network
    interface: str = "auto"            # auto = first non-loopback interface
    target_subnet: str = "auto"        # auto = derived from interface CIDR
    exclude_macs: List[str] = field(default_factory=list)
    exclude_ips: List[str] = field(default_factory=list)

    # Discovery
    discovery_interval: int = 60       # seconds between full ARP sweeps
    discovery_timeout: float = 3.0     # ARP timeout per sweep

    # Online/Offline
    online_check_interval: int = 10    # seconds between checks for ONLINE devices
    offline_check_interval: int = 120  # seconds between checks for OFFLINE devices (backoff)
    ping_timeout: float = 1.0          # ICMP timeout per device
    ping_count: int = 1               # ICMP echo requests per check
    max_offline_backoff: int = 600     # cap backoff at 10 min

    # Bandwidth
    bandwidth_interval: int = 5        # seconds between bandwidth samples
    use_iptables: bool = True          # per-device via iptables (requires root)
    iptables_chain: str = "NETPULSE_INPUT"

    # Database
    db_path: str = "/var/lib/netpulse/netpulse.db"
    db_retention_days: int = 90

    # Reporting
    export_dir: str = "/var/lib/netpulse/exports"
    json_export_interval: int = 300    # 5 min
    csv_export_interval: int = 3600    # 1 hr

    # Daemon
    pid_file: str = "/var/run/netpulse.pid"
    log_file: str = "/var/log/netpulse.log"
    log_level: str = "INFO"
    daemonize: bool = False


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


def _parse_simple_toml(text: str) -> Dict[str, Any]:
    """Minimal TOML-subset parser for flat `key = value` files (Python < 3.11).

    Netpulse's config file has no nested tables, so this covers it without
    a third-party dependency. Supports strings, ints, floats, bools, and
    single-line arrays of strings; '#' starts a comment.
    """
    result: Dict[str, Any] = {}
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.split("#", 1)[0].strip()
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
    text = file_path.read_text()
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
    return apply_cli_overrides(config, cli_overrides)
