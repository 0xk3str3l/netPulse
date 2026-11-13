"""Config precedence: dataclass defaults -> config file -> CLI flags."""

import os
import sys

import pytest

from netpulse.config import (
    Config,
    _parse_simple_toml,
    apply_cli_overrides,
    apply_config_file,
    load_config_file,
    resolve_config,
)


def test_defaults_are_netpulse_not_netmon():
    c = Config()
    assert "netmon" not in c.db_path.lower()
    assert "netpulse" in c.db_path.lower()
    assert c.db_path.endswith("netpulse.db")
    assert c.pid_file.endswith("netpulse.pid")
    assert c.log_file.endswith("netpulse.log")
    assert c.export_dir.endswith("exports")
    assert c.iptables_chain == "NETPULSE_INPUT"
    if os.name != "nt":
        assert c.db_path == "/var/lib/netpulse/netpulse.db"
        assert c.pid_file == "/var/run/netpulse.pid"
        assert c.log_file == "/var/log/netpulse.log"
        assert c.export_dir == "/var/lib/netpulse/exports"


def test_load_config_file_missing_returns_empty(tmp_path):
    assert load_config_file(str(tmp_path / "does-not-exist.conf")) == {}


def test_load_config_file_reads_toml(tmp_path):
    conf = tmp_path / "netpulse.conf"
    conf.write_text('discovery_interval = 30\nuse_iptables = false\n')
    assert load_config_file(str(conf)) == {
        "discovery_interval": 30,
        "use_iptables": False,
    }


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib is 3.11+")
def test_fallback_parser_matches_tomllib_on_the_example_file():
    import tomllib

    text = (
        'interface = "auto"\n'
        "discovery_interval = 60\n"
        "discovery_timeout = 3.0\n"
        "use_iptables = true\n"
        'exclude_ips = ["192.168.1.5", "192.168.1.6"]\n'
        "exclude_macs = []\n"
        "# a comment line\n"
        "\n"
    )
    assert _parse_simple_toml(text) == tomllib.loads(text)


def test_parse_simple_toml_value_types():
    text = (
        'a_string = "hello"\n'
        "an_int = 42\n"
        "a_float = 1.5\n"
        "a_true = true\n"
        "a_false = false\n"
        'a_list = ["x", "y"]\n'
        "an_empty_list = []\n"
    )
    assert _parse_simple_toml(text) == {
        "a_string": "hello",
        "an_int": 42,
        "a_float": 1.5,
        "a_true": True,
        "a_false": False,
        "a_list": ["x", "y"],
        "an_empty_list": [],
    }


def test_apply_config_file_overrides_only_given_keys():
    base = Config()
    merged = apply_config_file(base, {"discovery_interval": 15})
    assert merged.discovery_interval == 15
    assert merged.db_path == base.db_path  # untouched


def test_apply_config_file_rejects_unknown_key():
    with pytest.raises(ValueError, match="bogus_key"):
        apply_config_file(Config(), {"bogus_key": 1})


def test_apply_cli_overrides_ignores_none():
    base = Config(discovery_interval=15)
    merged = apply_cli_overrides(base, {"discovery_interval": None, "db_path": "/tmp/x.db"})
    assert merged.discovery_interval == 15  # None doesn't clobber the file value
    assert merged.db_path == "/tmp/x.db"


def test_resolve_config_precedence_defaults_file_cli(tmp_path):
    conf = tmp_path / "netpulse.conf"
    conf.write_text("discovery_interval = 30\nuse_iptables = false\n")

    # No file, no CLI -> pure defaults
    c = resolve_config(str(tmp_path / "missing.conf"), {})
    assert c.discovery_interval == 60

    # File overrides defaults
    c = resolve_config(str(conf), {})
    assert c.discovery_interval == 30
    assert c.use_iptables is False
    assert c.db_path == Config().db_path  # key not in file -> default survives

    # CLI overrides the file
    c = resolve_config(str(conf), {"discovery_interval": 999})
    assert c.discovery_interval == 999
    assert c.use_iptables is False  # untouched by CLI, file value still applies

    # Unset (None) CLI flags fall through to the file value
    c = resolve_config(str(conf), {"discovery_interval": None})
    assert c.discovery_interval == 30


def test_exclusions_match_mac_ip_and_cidr():
    c = Config(
        exclude_macs=["AA-BB-CC-DD-EE-FF"],
        exclude_ips=["192.168.2.50", "10.0.0.0/24"],
    )
    assert c.is_excluded("aa:bb:cc:dd:ee:ff", "192.168.2.1")
    assert c.is_excluded(ip="192.168.2.50")
    assert c.is_excluded(ip="10.0.0.9")
    assert not c.is_excluded("11:22:33:44:55:66", "192.168.2.9")


def test_validate_rejects_bad_modes():
    with pytest.raises(ValueError, match="discovery_mode"):
        Config(discovery_mode="sneaky").validate()
    with pytest.raises(ValueError, match="presence_mode"):
        Config(presence_mode="vibes").validate()
    with pytest.raises(ValueError, match="ping_workers"):
        Config(ping_workers=0).validate()


def test_fallback_parser_keeps_hash_inside_quoted_string():
    assert _parse_simple_toml('iptables_chain = "A#B"  # comment\n') == {
        "iptables_chain": "A#B"
    }
